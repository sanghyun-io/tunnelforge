use migration_core::{handle_request_streaming, CoreService, Request};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() {
    let stdin = io::stdin();
    let mut handled = false;
    let mut service = CoreService::new();

    for line in stdin.lock().lines() {
        match line {
            Ok(line) => {
                if line.trim().is_empty() {
                    continue;
                }
                handled = true;
                match serde_json::from_str::<Request>(&line) {
                    Ok(request) => {
                        let should_shutdown = request.command == "service.shutdown";
                        service.handle_request_streaming(request, emit_one);
                        if should_shutdown {
                            break;
                        }
                    }
                    Err(err) => emit_one(json!({
                        "event": "error",
                        "message": format!("invalid request JSON: {err}")
                    })),
                }
            }
            Err(err) => {
                emit_one(json!({"event": "error", "message": format!("stdin read failed: {err}")}));
            }
        }
    }

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
        );
    }
}

fn emit_one(event: Value) {
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{event}");
    let _ = stdout.flush();
}
