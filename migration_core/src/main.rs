use migration_core::{
    encode_protocol_frames, handle_request_streaming, protocol_error_event, CoreService,
    IntoProtocolEmitResult, ProtocolEmitError, ProtocolEmitResult, Request,
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
                request_id: None,
                payload: json!({}),
            },
            emit_one,
        )?;
    }
    Ok(())
}

fn process_jsonl_reader<R, F, O>(
    reader: R,
    service: &mut CoreService,
    mut emit: F,
) -> bool
where
    R: BufRead,
    F: FnMut(Value) -> O,
    O: IntoProtocolEmitResult,
{
    let mut handled = false;

    for line in reader.lines() {
        match line {
            Ok(line) => {
                if line.trim().is_empty() {
                    continue;
                }
                handled = true;
                match serde_json::from_str::<Request>(&line) {
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
        }
    }

    handled
}

fn emit_one(event: Value) -> ProtocolEmitResult {
    let mut stdout = io::stdout().lock();
    for frame in encode_protocol_frames(&event)? {
        stdout
            .write_all(&frame)
            .map_err(|err| ProtocolEmitError::io(format!("stdout write failed: {err}")))?;
    }
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
        assert_protocol_error(&events[0], Value::Null);
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
}
