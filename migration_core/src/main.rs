use migration_core::{
    encode_protocol_frames, handle_request_streaming, protocol_error_event, CoreService,
    IntoProtocolEmitResult, ProtocolEmitError, ProtocolEmitResult, Request,
    MAX_JSONL_FRAME_BYTES,
};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() -> ProtocolEmitResult {
    let stdin = io::stdin();
    let mut service = CoreService::new();
    let handled = process_jsonl_reader(stdin.lock(), &mut service, emit_one);

    if !handled {
        let command = std::env::args()
            .nth(1)
            .unwrap_or_else(|| "inspect".to_string());
        handle_request_streaming(
            Request {
                command,
                request_id: Some("cli-compat".to_string()),
                payload: json!({}),
            },
            emit_one,
        )?;
    }
    Ok(())
}

fn process_jsonl_reader<R, F, O>(
    mut reader: R,
    service: &mut CoreService,
    mut emit: F,
) -> bool
where
    R: BufRead,
    F: FnMut(Value) -> O,
    O: IntoProtocolEmitResult,
{
    let mut handled = false;

    loop {
        let frame = match read_bounded_jsonl_frame(&mut reader) {
            Ok(BoundedJsonlFrame::Eof) => break,
            Ok(frame) => frame,
            Err(err) => {
                handled = true;
                let _ = emit(protocol_error_event(
                    None,
                    "stdin_read_failed",
                    format!("stdin read failed: {err}"),
                ))
                .into_protocol_emit_result();
                break;
            }
        };
        match frame {
            BoundedJsonlFrame::Frame(bytes) => {
                if bytes.iter().all(|byte| byte.is_ascii_whitespace()) {
                    continue;
                }
                handled = true;
                let line = match std::str::from_utf8(&bytes) {
                    Ok(line) => line,
                    Err(err) => {
                        if emit(protocol_error_event(
                            None,
                            "invalid_request_utf8",
                            format!("request frame is not valid UTF-8: {err}"),
                        ))
                        .into_protocol_emit_result()
                        .is_err()
                        {
                            break;
                        }
                        continue;
                    }
                };
                match serde_json::from_str::<Request>(line) {
                    Ok(request) => {
                        let should_shutdown = request.command == "service.shutdown";
                        if service
                            .handle_request_streaming(request, &mut emit)
                            .is_err()
                            || should_shutdown
                        {
                            break;
                        }
                    }
                    Err(err) => {
                        if emit(protocol_error_event(
                            None,
                            "invalid_request_json",
                            format!("invalid request JSON: {err}"),
                        ))
                        .into_protocol_emit_result()
                        .is_err()
                        {
                            break;
                        }
                    }
                }
            }
            BoundedJsonlFrame::Oversized => {
                handled = true;
                if emit(protocol_error_event(
                    None,
                    "jsonl_frame_too_large",
                    "request JSONL frame exceeds MAX_JSONL_FRAME_BYTES",
                ))
                .into_protocol_emit_result()
                .is_err()
                {
                    break;
                }
            }
            BoundedJsonlFrame::Unterminated => {
                handled = true;
                let _ = emit(protocol_error_event(
                    None,
                    "jsonl_frame_missing_newline",
                    "request JSONL frame is missing its newline terminator",
                ))
                .into_protocol_emit_result();
                break;
            }
            BoundedJsonlFrame::Eof => break,
        }
    }

    handled
}

enum BoundedJsonlFrame {
    Frame(Vec<u8>),
    Oversized,
    Unterminated,
    Eof,
}

fn read_bounded_jsonl_frame<R: BufRead>(reader: &mut R) -> io::Result<BoundedJsonlFrame> {
    let mut frame = Vec::new();
    let mut oversized = false;
    let mut saw_bytes = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if !saw_bytes {
                Ok(BoundedJsonlFrame::Eof)
            } else if oversized {
                Ok(BoundedJsonlFrame::Oversized)
            } else {
                Ok(BoundedJsonlFrame::Unterminated)
            };
        }
        saw_bytes = true;
        let newline = available.iter().position(|byte| *byte == b'\n');
        let consumed = newline.map_or(available.len(), |index| index + 1);
        if !oversized {
            if consumed > MAX_JSONL_FRAME_BYTES - frame.len() {
                oversized = true;
            } else {
                frame.extend_from_slice(&available[..consumed]);
            }
        }
        reader.consume(consumed);
        if newline.is_some() {
            return if oversized {
                Ok(BoundedJsonlFrame::Oversized)
            } else {
                Ok(BoundedJsonlFrame::Frame(frame))
            };
        }
    }
}

fn emit_one(event: Value) -> ProtocolEmitResult {
    let mut stdout = io::stdout().lock();
    encode_protocol_frames(&event, |frame| {
        stdout
            .write_all(frame)
            .map_err(|err| ProtocolEmitError::io(format!("stdout write failed: {err}")))
    })?;
    stdout
        .flush()
        .map_err(|err| ProtocolEmitError::io(format!("stdout flush failed: {err}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, BufRead, Read};

    struct FailingReader;

    impl Read for FailingReader {
        fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("simulated stdin failure"))
        }
    }

    impl BufRead for FailingReader {
        fn fill_buf(&mut self) -> io::Result<&[u8]> {
            Err(io::Error::other("simulated stdin failure"))
        }

        fn consume(&mut self, _amount: usize) {}
    }

    fn assert_protocol_error(event: &Value, request_id: Value) {
        assert_eq!(event["event"], "error");
        assert_eq!(event["request_id"], request_id);
        assert!(event.get("command").is_some());
        assert!(matches!(event["code"].as_str(), Some(value) if !value.is_empty()));
        assert!(matches!(event["message"].as_str(), Some(value) if !value.is_empty()));
    }

    #[test]
    fn stdin_read_failure_uses_protocol_error_envelope() {
        let mut events = Vec::new();
        let mut service = CoreService::new();

        let handled = process_jsonl_reader(FailingReader, &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(events.len(), 1);
        assert_protocol_error(&events[0], json!("protocol-invalid-request-id"));
    }

    #[test]
    fn parsed_lines_dispatch_through_stateful_core_service() {
        let mut events = Vec::new();
        let mut service = CoreService::new();
        let input = b"{\"command\":\"query.execute\",\"request_id\":\"stateful-1\",\"payload\":{\"connection_id\":\"missing\",\"sql\":\"SELECT 1\"}}\n";

        let handled = process_jsonl_reader(&input[..], &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(events.len(), 1);
        assert_protocol_error(&events[0], json!("stateful-1"));
        assert!(events[0]["message"]
            .as_str()
            .unwrap()
            .contains("unknown connection_id: missing"));
    }

    #[test]
    fn frame_emit_failure_stops_the_request_loop() {
        let mut service = CoreService::new();
        let input = b"{\"command\":\"service.hello\",\"request_id\":\"first\",\"payload\":{}}\n\
                      {\"command\":\"service.hello\",\"request_id\":\"second\",\"payload\":{}}\n";
        let mut calls = 0;

        let handled = process_jsonl_reader(&input[..], &mut service, |_event| {
            calls += 1;
            Err(ProtocolEmitError::io("simulated broken pipe"))
        });

        assert!(handled);
        assert_eq!(calls, 1);
    }

    #[test]
    fn exact_maximum_jsonl_frame_including_newline_is_accepted() {
        let prefix = b"{\"command\":\"unknown.command\",\"request_id\":\"exact\",\"payload\":{\"padding\":\"";
        let suffix = b"\"}}\n";
        let padding = MAX_JSONL_FRAME_BYTES - prefix.len() - suffix.len();
        let mut input = Vec::with_capacity(MAX_JSONL_FRAME_BYTES);
        input.extend_from_slice(prefix);
        input.extend(std::iter::repeat(b'x').take(padding));
        input.extend_from_slice(suffix);
        let mut events = Vec::new();
        let mut service = CoreService::new();

        let handled = process_jsonl_reader(&input[..], &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(input.len(), MAX_JSONL_FRAME_BYTES);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["command"], "unknown.command");
    }

    #[test]
    fn oversized_jsonl_frame_is_drained_and_next_request_is_processed() {
        let mut input = vec![b'x'; MAX_JSONL_FRAME_BYTES];
        input.extend_from_slice(b"x\n");
        input.extend_from_slice(
            b"{\"command\":\"service.hello\",\"request_id\":\"after-large\",\"payload\":{}}\n",
        );
        let mut events = Vec::new();
        let mut service = CoreService::new();

        let handled = process_jsonl_reader(&input[..], &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["code"], "jsonl_frame_too_large");
        assert_eq!(events[0]["command"], Value::Null);
        assert_eq!(events[1]["request_id"], "after-large");
        assert_eq!(events[1]["command"], "service.hello");
    }

    #[test]
    fn invalid_utf8_frame_is_structured_and_next_request_is_processed() {
        let mut input = vec![0xff, b'\n'];
        input.extend_from_slice(
            b"{\"command\":\"service.hello\",\"request_id\":\"after-utf8\",\"payload\":{}}\n",
        );
        let mut events = Vec::new();
        let mut service = CoreService::new();

        let handled = process_jsonl_reader(&input[..], &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["code"], "invalid_request_utf8");
        assert_eq!(events[1]["request_id"], "after-utf8");
    }

    #[test]
    fn unterminated_jsonl_frame_is_rejected_instead_of_parsed() {
        let input = b"{\"command\":\"service.hello\",\"request_id\":\"unterminated\",\"payload\":{}}";
        let mut events = Vec::new();
        let mut service = CoreService::new();

        let handled = process_jsonl_reader(&input[..], &mut service, |event| events.push(event));

        assert!(handled);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["code"], "jsonl_frame_missing_newline");
        assert_eq!(events[0]["command"], Value::Null);
    }
}
