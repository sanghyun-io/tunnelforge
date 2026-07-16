use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt;
use std::io::{self, Write};
#[cfg(test)]
use std::cell::Cell;

use crate::*;

pub const PROTOCOL_VERSION: u32 = 1;
pub const PROCESS_VERSION: u32 = 1;
pub const MAX_JSONL_FRAME_BYTES: usize = 1_048_576;
pub const MAX_ASSEMBLED_EVENT_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_ASSEMBLED_EVENT_CHUNKS: usize = 4_096;
pub const MAX_ASSEMBLED_EVENT_NODES: usize = 65_536;
pub const MAX_ASSEMBLED_EVENT_DEPTH: usize = 128;
const MAX_REFERENCE_PAGE_ITEMS: usize = 64;
const PROTOCOL_ERROR_REQUEST_ID: &str = "protocol-invalid-request-id";

pub const PROCESS_CAPABILITIES: &[&str] = &[
    "request.deadline",
    "request.strict_id",
    "process.generation",
    "mutation.outcome_indeterminate",
];

pub const PUBLIC_COMMANDS: &[&str] = &[
    "connection.open",
    "connection.close",
    "connection.test",
    "schema.list",
    "schema.inspect",
    "schema.diff",
    "query.execute",
    "query.cancel",
    "dump.run",
    "dump.import",
    "migration.plan",
    "migration.run",
    "migration.verify",
    "migration.resume",
    "migration.cleanup",
    "oneclick.preflight",
    "oneclick.analyze",
    "oneclick.plan",
    "oneclick.validate",
    "oneclick.report",
    "job.cancel",
    "service.shutdown",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolEmitError {
    code: &'static str,
    message: String,
    side_effect_started: bool,
}

impl ProtocolEmitError {
    pub fn io(message: impl Into<String>) -> Self {
        Self {
            code: "protocol_emit_failed",
            message: message.into(),
            side_effect_started: false,
        }
    }

    fn aggregate(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            side_effect_started: false,
        }
    }

    pub fn code(&self) -> &'static str {
        self.code
    }

    pub fn after_side_effect(mut self) -> Self {
        self.side_effect_started = true;
        self
    }

    pub fn side_effect_started(&self) -> bool {
        self.side_effect_started
    }

    pub(crate) fn with_secondary_failure(
        mut self,
        context: &str,
        failure: impl fmt::Display,
    ) -> Self {
        self.message = format!("{}; {context}: {failure}", self.message);
        self
    }
}

impl fmt::Display for ProtocolEmitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ProtocolEmitError {}

pub type ProtocolEmitResult = Result<(), ProtocolEmitError>;

pub trait IntoProtocolEmitResult {
    fn into_protocol_emit_result(self) -> ProtocolEmitResult;
}

impl IntoProtocolEmitResult for () {
    fn into_protocol_emit_result(self) -> ProtocolEmitResult {
        Ok(())
    }
}

impl IntoProtocolEmitResult for ProtocolEmitResult {
    fn into_protocol_emit_result(self) -> ProtocolEmitResult {
        self
    }
}

#[cfg(test)]
#[derive(Debug, Clone, Copy)]
struct ProtocolEncodingMetrics {
    serialized_frames: usize,
    serialized_bytes: usize,
    emitted_frames: usize,
    peak_buffered_frames: usize,
    peak_buffered_bytes: usize,
    visited_nodes: usize,
    peak_reference_page_items: usize,
}

#[cfg(test)]
thread_local! {
    static SERIALIZED_FRAMES: Cell<usize> = const { Cell::new(0) };
    static SERIALIZED_BYTES: Cell<usize> = const { Cell::new(0) };
    static EMITTED_FRAMES: Cell<usize> = const { Cell::new(0) };
    static PEAK_BUFFERED_FRAMES: Cell<usize> = const { Cell::new(0) };
    static PEAK_BUFFERED_BYTES: Cell<usize> = const { Cell::new(0) };
    static VISITED_NODES: Cell<usize> = const { Cell::new(0) };
    static PEAK_REFERENCE_PAGE_ITEMS: Cell<usize> = const { Cell::new(0) };
}

fn record_frame_serialization(bytes: usize) {
    #[cfg(test)]
    {
        SERIALIZED_FRAMES.set(SERIALIZED_FRAMES.get() + 1);
        SERIALIZED_BYTES.set(SERIALIZED_BYTES.get() + bytes);
        PEAK_BUFFERED_FRAMES.set(PEAK_BUFFERED_FRAMES.get().max(1));
        PEAK_BUFFERED_BYTES.set(PEAK_BUFFERED_BYTES.get().max(bytes));
    }
    #[cfg(not(test))]
    let _ = bytes;
}

fn record_value_serialization(bytes: usize) {
    #[cfg(test)]
    SERIALIZED_BYTES.set(SERIALIZED_BYTES.get() + bytes);
    #[cfg(not(test))]
    let _ = bytes;
}

fn record_buffered_bytes(bytes: usize) {
    #[cfg(test)]
    PEAK_BUFFERED_BYTES.set(PEAK_BUFFERED_BYTES.get().max(bytes));
    #[cfg(not(test))]
    let _ = bytes;
}

fn record_frame_emitted() {
    #[cfg(test)]
    EMITTED_FRAMES.set(EMITTED_FRAMES.get() + 1);
}

fn record_node_visited() {
    #[cfg(test)]
    VISITED_NODES.set(VISITED_NODES.get() + 1);
}

fn record_reference_page_items(items: usize) {
    #[cfg(test)]
    PEAK_REFERENCE_PAGE_ITEMS.set(PEAK_REFERENCE_PAGE_ITEMS.get().max(items));
    #[cfg(not(test))]
    let _ = items;
}

#[cfg(test)]
fn reset_protocol_encoding_metrics() {
    SERIALIZED_FRAMES.set(0);
    SERIALIZED_BYTES.set(0);
    EMITTED_FRAMES.set(0);
    PEAK_BUFFERED_FRAMES.set(0);
    PEAK_BUFFERED_BYTES.set(0);
    VISITED_NODES.set(0);
    PEAK_REFERENCE_PAGE_ITEMS.set(0);
}

#[cfg(test)]
fn protocol_encoding_metrics() -> ProtocolEncodingMetrics {
    ProtocolEncodingMetrics {
        serialized_frames: SERIALIZED_FRAMES.get(),
        serialized_bytes: SERIALIZED_BYTES.get(),
        emitted_frames: EMITTED_FRAMES.get(),
        peak_buffered_frames: PEAK_BUFFERED_FRAMES.get(),
        peak_buffered_bytes: PEAK_BUFFERED_BYTES.get(),
        visited_nodes: VISITED_NODES.get(),
        peak_reference_page_items: PEAK_REFERENCE_PAGE_ITEMS.get(),
    }
}

struct BoundedJsonBuffer {
    bytes: Vec<u8>,
    limit: usize,
    exceeded: bool,
}

impl BoundedJsonBuffer {
    fn new(limit: usize) -> Self {
        Self {
            bytes: Vec::with_capacity(limit.min(64 * 1024)),
            limit,
            exceeded: false,
        }
    }
}

impl Write for BoundedJsonBuffer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit - self.bytes.len() {
            self.exceeded = true;
            return Err(io::Error::other("protocol frame exceeds bounded buffer"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Debug)]
struct ProtocolChunkNode {
    node_id: u64,
    parent_node_id: Option<u64>,
    slot_index: Option<usize>,
    value_kind: &'static str,
}

struct ProtocolChunkEncoder {
    request_id: Value,
    command: Value,
    logical_event: String,
    next_node_id: u64,
    max_frame_bytes: usize,
    aggregate_bytes: usize,
    chunk_count: usize,
    node_count: usize,
    track_metrics: bool,
}

impl ProtocolChunkEncoder {
    fn new(
        event: &Value,
        max_frame_bytes: usize,
        track_metrics: bool,
    ) -> Result<Self, ProtocolEmitError> {
        let fields = event
            .as_object()
            .ok_or_else(|| ProtocolEmitError::io("protocol event must be an object"))?;
        let logical_event = fields
            .get("event")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty() && *value != "payload_chunk")
            .ok_or_else(|| ProtocolEmitError::io("protocol event is missing event type"))?
            .to_string();
        Ok(Self {
            request_id: fields.get("request_id").cloned().unwrap_or(Value::Null),
            command: fields.get("command").cloned().unwrap_or(Value::Null),
            logical_event,
            next_node_id: 0,
            max_frame_bytes,
            aggregate_bytes: 0,
            chunk_count: 0,
            node_count: 0,
            track_metrics,
        })
    }

    fn visit_and_emit<F>(
        &mut self,
        value: &Value,
        parent_node_id: Option<u64>,
        slot_index: Option<usize>,
        depth: usize,
        emit: &mut F,
    ) -> Result<u64, ProtocolEmitError>
    where
        F: FnMut(&[u8]) -> ProtocolEmitResult,
    {
        if depth > MAX_ASSEMBLED_EVENT_DEPTH {
            return Err(ProtocolEmitError::aggregate(
                "protocol_aggregate_depth_exceeded",
                "protocol event exceeds aggregate reconstruction depth limit",
            ));
        }
        if self.node_count >= MAX_ASSEMBLED_EVENT_NODES {
            return Err(ProtocolEmitError::aggregate(
                "protocol_aggregate_nodes_exceeded",
                "protocol event exceeds aggregate node count limit",
            ));
        }
        self.node_count += 1;
        if self.track_metrics {
            record_node_visited();
        }
        let node_id = self.next_node_id;
        self.next_node_id += 1;
        match value {
            Value::Object(fields) => {
                let node = ProtocolChunkNode {
                    node_id,
                    parent_node_id,
                    slot_index,
                    value_kind: "object",
                };
                let mut items = Vec::with_capacity(fields.len().min(MAX_REFERENCE_PAGE_ITEMS));
                let mut sequence = 0usize;
                for (index, (key, child)) in fields.iter().enumerate() {
                    let key_value = Value::String(key.clone());
                    let key_node_id = self.visit_and_emit(
                        &key_value,
                        Some(node_id),
                        Some(index),
                        depth + 1,
                        emit,
                    )?;
                    let value_node_id = self.visit_and_emit(
                        child,
                        Some(node_id),
                        Some(index),
                        depth + 1,
                        emit,
                    )?;
                    items.push(json!({
                        "key_node_id": key_node_id,
                        "value_node_id": value_node_id
                    }));
                    if items.len() == MAX_REFERENCE_PAGE_ITEMS {
                        if self.track_metrics {
                            record_reference_page_items(items.len());
                        }
                        let final_page = index + 1 == fields.len();
                        self.item_page(&node, &mut sequence, items, final_page, emit)?;
                        items = Vec::with_capacity(MAX_REFERENCE_PAGE_ITEMS);
                    }
                }
                if !items.is_empty() || fields.is_empty() {
                    if self.track_metrics {
                        record_reference_page_items(items.len());
                    }
                    self.item_page(&node, &mut sequence, items, true, emit)?;
                }
            }
            Value::Array(values) => {
                let node = ProtocolChunkNode {
                    node_id,
                    parent_node_id,
                    slot_index,
                    value_kind: "list",
                };
                let mut items = Vec::with_capacity(values.len().min(MAX_REFERENCE_PAGE_ITEMS));
                let mut sequence = 0usize;
                for (index, child) in values.iter().enumerate() {
                    items.push(Value::from(self.visit_and_emit(
                        child,
                        Some(node_id),
                        Some(index),
                        depth + 1,
                        emit,
                    )?));
                    if items.len() == MAX_REFERENCE_PAGE_ITEMS {
                        if self.track_metrics {
                            record_reference_page_items(items.len());
                        }
                        let final_page = index + 1 == values.len();
                        self.item_page(&node, &mut sequence, items, final_page, emit)?;
                        items = Vec::with_capacity(MAX_REFERENCE_PAGE_ITEMS);
                    }
                }
                if !items.is_empty() || values.is_empty() {
                    if self.track_metrics {
                        record_reference_page_items(items.len());
                    }
                    self.item_page(&node, &mut sequence, items, true, emit)?;
                }
            }
            Value::String(text) => {
                let node = ProtocolChunkNode {
                    node_id,
                    parent_node_id,
                    slot_index,
                    value_kind: "utf8_string",
                };
                self.string_frames(&node, text, emit)?;
            }
            _ => {
                let node = ProtocolChunkNode {
                    node_id,
                    parent_node_id,
                    slot_index,
                    value_kind: "atomic",
                };
                let mut sequence = 0;
                self.item_page(&node, &mut sequence, vec![value.clone()], true, emit)?;
            }
        }
        Ok(node_id)
    }

    fn base_frame(&self, node: &ProtocolChunkNode, sequence: usize, final_chunk: bool) -> Value {
        json!({
            "event": "payload_chunk",
            "request_id": self.request_id,
            "command": self.command,
            "logical_event": self.logical_event,
            "node_id": node.node_id,
            "parent_node_id": node.parent_node_id,
            "slot_index": node.slot_index,
            "sequence": sequence,
            "final": final_chunk,
            "value_kind": node.value_kind
        })
    }

    fn serialize_frame(&self, frame: &Value) -> Result<Vec<u8>, ProtocolEmitError> {
        let mut bytes = serde_json::to_vec(frame)
            .map_err(|err| ProtocolEmitError::io(format!("protocol frame encode failed: {err}")))?;
        bytes.push(b'\n');
        if self.track_metrics {
            record_frame_serialization(bytes.len());
        }
        if bytes.len() > self.max_frame_bytes {
            return Err(ProtocolEmitError::io(
                "protocol frame exceeds MAX_JSONL_FRAME_BYTES",
            ));
        }
        Ok(bytes)
    }

    fn emit_frame<F>(&mut self, frame: &Value, emit: &mut F) -> ProtocolEmitResult
    where
        F: FnMut(&[u8]) -> ProtocolEmitResult,
    {
        let bytes = self.serialize_frame(frame)?;
        if self.chunk_count >= MAX_ASSEMBLED_EVENT_CHUNKS {
            return Err(ProtocolEmitError::aggregate(
                "protocol_aggregate_chunks_exceeded",
                "protocol event exceeds aggregate chunk count limit",
            ));
        }
        let aggregate_bytes = self.aggregate_bytes.checked_add(bytes.len()).ok_or_else(|| {
            ProtocolEmitError::aggregate(
                "protocol_aggregate_bytes_exceeded",
                "protocol event exceeds aggregate byte limit",
            )
        })?;
        if aggregate_bytes > MAX_ASSEMBLED_EVENT_BYTES {
            return Err(ProtocolEmitError::aggregate(
                "protocol_aggregate_bytes_exceeded",
                "protocol event exceeds aggregate byte limit",
            ));
        }
        self.chunk_count += 1;
        self.aggregate_bytes = aggregate_bytes;
        emit(&bytes)?;
        if self.track_metrics {
            record_frame_emitted();
        }
        Ok(())
    }

    fn item_page<F>(
        &mut self,
        node: &ProtocolChunkNode,
        sequence: &mut usize,
        items: Vec<Value>,
        final_page: bool,
        emit: &mut F,
    ) -> ProtocolEmitResult
    where
        F: FnMut(&[u8]) -> ProtocolEmitResult,
    {
        let mut empty_false = self.base_frame(node, 0, false);
        empty_false
            .as_object_mut()
            .expect("chunk frame object")
            .insert("items".to_string(), Value::Array(Vec::new()));
        let false_base_len = self.serialize_frame(&empty_false)?.len();
        let mut empty_true = self.base_frame(node, 0, true);
        empty_true
            .as_object_mut()
            .expect("chunk frame object")
            .insert("items".to_string(), Value::Array(Vec::new()));
        let true_base_len = self.serialize_frame(&empty_true)?.len();

        if items.is_empty() {
            if !final_page {
                return Ok(());
            }
            empty_true["sequence"] = Value::from(*sequence);
            *sequence += 1;
            return self.emit_frame(&empty_true, emit);
        }

        let item_count = items.len();
        let mut current = Vec::new();
        let mut current_serialized_bytes = 0usize;
        for (index, item) in items.into_iter().enumerate() {
            let item_bytes = serde_json::to_vec(&item).map_err(|err| {
                ProtocolEmitError::io(format!("protocol collection item encode failed: {err}"))
            })?;
            if self.track_metrics {
                record_value_serialization(item_bytes.len());
            }
            let separator_bytes = usize::from(!current.is_empty());
            let is_final_item = final_page && index + 1 == item_count;
            let candidate_base = if is_final_item {
                true_base_len
            } else {
                false_base_len
            } + Self::sequence_width_extra(*sequence);
            let candidate_len = candidate_base
                + current_serialized_bytes
                + separator_bytes
                + item_bytes.len();
            if candidate_len > self.max_frame_bytes {
                if current.is_empty() {
                    return Err(ProtocolEmitError::io(
                        "protocol collection item cannot fit in one frame",
                    ));
                }
                let mut frame = self.base_frame(node, *sequence, false);
                frame
                    .as_object_mut()
                    .expect("chunk frame object")
                    .insert("items".to_string(), Value::Array(std::mem::take(&mut current)));
                self.emit_frame(&frame, emit)?;
                *sequence += 1;
                current_serialized_bytes = 0;
                let standalone_base = if is_final_item {
                    true_base_len
                } else {
                    false_base_len
                } + Self::sequence_width_extra(*sequence);
                if standalone_base + item_bytes.len() > self.max_frame_bytes {
                    return Err(ProtocolEmitError::io(
                        "protocol collection item cannot fit in one frame",
                    ));
                }
            }
            current_serialized_bytes += usize::from(!current.is_empty()) + item_bytes.len();
            current.push(item);
        }

        let mut frame = self.base_frame(node, *sequence, final_page);
        frame
            .as_object_mut()
            .expect("chunk frame object")
            .insert("items".to_string(), Value::Array(current));
        *sequence += 1;
        self.emit_frame(&frame, emit)
    }

    fn json_escaped_char_len(value: char) -> usize {
        match value {
            '"' | '\\' | '\u{0008}' | '\u{0009}' | '\u{000A}' | '\u{000C}' | '\u{000D}' => 2,
            '\u{0000}'..='\u{001F}' => 6,
            _ => value.len_utf8(),
        }
    }

    fn sequence_width_extra(sequence: usize) -> usize {
        let mut value = sequence;
        let mut digits = 1usize;
        while value >= 10 {
            value /= 10;
            digits += 1;
        }
        digits - 1
    }

    fn string_frames<F>(
        &mut self,
        node: &ProtocolChunkNode,
        text: &str,
        emit: &mut F,
    ) -> ProtocolEmitResult
    where
        F: FnMut(&[u8]) -> ProtocolEmitResult,
    {
        let mut empty_false = self.base_frame(node, 0, false);
        empty_false
            .as_object_mut()
            .expect("chunk frame object")
            .insert("text".to_string(), Value::String(String::new()));
        let false_base_len = self.serialize_frame(&empty_false)?.len();
        let mut empty_true = self.base_frame(node, 0, true);
        empty_true
            .as_object_mut()
            .expect("chunk frame object")
            .insert("text".to_string(), Value::String(String::new()));
        let true_base_len = self.serialize_frame(&empty_true)?.len();
        if text.is_empty() {
            return self.emit_frame(&empty_true, emit);
        }
        let mut start = 0usize;
        let mut escaped_bytes = 0usize;
        let mut sequence = 0usize;
        for (index, value) in text.char_indices() {
            let value_len = Self::json_escaped_char_len(value);
            let false_capacity = self
                .max_frame_bytes
                .checked_sub(false_base_len + Self::sequence_width_extra(sequence))
                .ok_or_else(|| {
                    ProtocolEmitError::io("protocol string frame metadata exceeds wire cap")
                })?;
            if escaped_bytes + value_len > false_capacity {
                if index == start {
                    return Err(ProtocolEmitError::io(
                        "one UTF-8 code point cannot fit in a protocol frame",
                    ));
                }
                let mut frame = self.base_frame(node, sequence, false);
                frame.as_object_mut().expect("chunk frame object").insert(
                    "text".to_string(),
                    Value::String(text[start..index].to_string()),
                );
                self.emit_frame(&frame, emit)?;
                sequence += 1;
                start = index;
                escaped_bytes = 0;
                let next_capacity = self
                    .max_frame_bytes
                    .checked_sub(false_base_len + Self::sequence_width_extra(sequence))
                    .ok_or_else(|| {
                        ProtocolEmitError::io("protocol string frame metadata exceeds wire cap")
                    })?;
                if value_len > next_capacity {
                    return Err(ProtocolEmitError::io(
                        "one UTF-8 code point cannot fit in a protocol frame",
                    ));
                }
            }
            escaped_bytes += value_len;
        }
        let true_capacity = self
            .max_frame_bytes
            .checked_sub(true_base_len + Self::sequence_width_extra(sequence))
            .ok_or_else(|| {
                ProtocolEmitError::io("protocol string frame metadata exceeds wire cap")
            })?;
        if escaped_bytes > true_capacity {
            return Err(ProtocolEmitError::io(
                "final UTF-8 protocol chunk cannot fit in one frame",
            ));
        }
        let mut frame = self.base_frame(node, sequence, true);
        frame.as_object_mut().expect("chunk frame object").insert(
            "text".to_string(),
            Value::String(text[start..].to_string()),
        );
        self.emit_frame(&frame, emit)
    }

    fn finish<F>(mut self, event: &Value, emit: &mut F) -> ProtocolEmitResult
    where
        F: FnMut(&[u8]) -> ProtocolEmitResult,
    {
        let root = self.visit_and_emit(event, None, None, 1, emit)?;
        if root != 0 {
            return Err(ProtocolEmitError::io("protocol root node id is invalid"));
        }
        Ok(())
    }
}

pub fn encode_protocol_frames<F>(event: &Value, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(&[u8]) -> ProtocolEmitResult,
{
    encode_protocol_frames_with_limit(event, MAX_JSONL_FRAME_BYTES, &mut emit)
}

fn discard_protocol_frame(_frame: &[u8]) -> ProtocolEmitResult {
    Ok(())
}

fn count_aggregate_node(depth: usize, nodes: &mut usize) -> ProtocolEmitResult {
    if depth > MAX_ASSEMBLED_EVENT_DEPTH {
        return Err(ProtocolEmitError::aggregate(
            "protocol_aggregate_depth_exceeded",
            "protocol event exceeds aggregate reconstruction depth limit",
        ));
    }
    if *nodes >= MAX_ASSEMBLED_EVENT_NODES {
        return Err(ProtocolEmitError::aggregate(
            "protocol_aggregate_nodes_exceeded",
            "protocol event exceeds aggregate node count limit",
        ));
    }
    *nodes += 1;
    Ok(())
}

fn validate_aggregate_shape(value: &Value, depth: usize, nodes: &mut usize) -> ProtocolEmitResult {
    count_aggregate_node(depth, nodes)?;
    match value {
        Value::Object(fields) => {
            for child in fields.values() {
                count_aggregate_node(depth + 1, nodes)?;
                validate_aggregate_shape(child, depth + 1, nodes)?;
            }
        }
        Value::Array(values) => {
            for child in values {
                validate_aggregate_shape(child, depth + 1, nodes)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn encode_protocol_frames_with_limit<F>(
    event: &Value,
    max_frame_bytes: usize,
    emit: &mut F,
) -> ProtocolEmitResult
where
    F: FnMut(&[u8]) -> ProtocolEmitResult,
{
    let mut structural_nodes = 0;
    validate_aggregate_shape(event, 1, &mut structural_nodes)?;
    let json_limit = max_frame_bytes.checked_sub(1).ok_or_else(|| {
        ProtocolEmitError::io("MAX_JSONL_FRAME_BYTES cannot fit a JSONL newline")
    })?;
    let mut direct = BoundedJsonBuffer::new(json_limit);
    match serde_json::to_writer(&mut direct, event) {
        Ok(()) => {
            direct.bytes.push(b'\n');
            record_frame_serialization(direct.bytes.len());
            emit(&direct.bytes)?;
            record_frame_emitted();
            return Ok(());
        }
        Err(err) if !direct.exceeded => {
            return Err(ProtocolEmitError::io(format!(
                "protocol event encode failed: {err}"
            )));
        }
        Err(_) => {}
    }
    record_value_serialization(direct.bytes.len());
    record_buffered_bytes(direct.bytes.len());
    drop(direct);
    let mut count_only = discard_protocol_frame;
    ProtocolChunkEncoder::new(event, max_frame_bytes, false)?
        .finish(event, &mut count_only)?;
    ProtocolChunkEncoder::new(event, max_frame_bytes, true)?.finish(event, emit)
}

pub struct CoreService {
    connections: BTreeMap<String, LiveAdapter>,
    next_connection_sequence: u64,
}

impl CoreService {
    pub fn new() -> Self {
        Self {
            connections: BTreeMap::new(),
            next_connection_sequence: 1,
        }
    }

    pub fn handle_request_streaming<F, R>(
        &mut self,
        request: Request,
        mut emit: F,
    ) -> ProtocolEmitResult
    where
        F: FnMut(Value) -> R,
        R: IntoProtocolEmitResult,
    {
        let request_id = request.request_id.clone();
        let command = request.command.clone();
        let mut failed = false;
        self.handle_request_streaming_unchecked(request, |event| {
            if failed {
                return Err(ProtocolEmitError::io("protocol emitter is fused"));
            }
            let result = emit(normalize_protocol_event(event, &request_id, &command))
                .into_protocol_emit_result();
            if result.is_err() {
                failed = true;
            }
            result
        })
    }

    fn handle_request_streaming_unchecked<F, R>(
        &mut self,
        request: Request,
        emit: F,
    ) -> ProtocolEmitResult
    where
        F: FnMut(Value) -> R,
        R: IntoProtocolEmitResult,
    {
        match request.command.as_str() {
            "connection.open" => {
                let (events, side_effect_started) = self.connection_open(&request);
                emit_operation_events(events, side_effect_started, emit)
            }
            "connection.close" => {
                let (events, side_effect_started) = self.connection_close(&request);
                emit_operation_events(events, side_effect_started, emit)
            }
            "query.execute" => {
                let (events, side_effect_started) = self.query_execute(&request);
                emit_operation_events(events, side_effect_started, emit)
            }
            "service.shutdown" => {
                self.connections.clear();
                emit_all_events(service_shutdown(&request), emit)
                    .map_err(ProtocolEmitError::after_side_effect)
            }
            _ => handle_request_streaming_unchecked(request, emit),
        }
    }

    fn connection_open(&mut self, request: &Request) -> (Vec<Value>, bool) {
        let endpoint = match request_endpoint(request) {
            Ok(endpoint) => endpoint,
            Err(err) => {
                return (vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })], false)
            }
        };
        match LiveAdapter::connect(&endpoint) {
            Ok(adapter) => {
                let id = unique_connection_id(&endpoint, self.next_connection_sequence);
                self.next_connection_sequence = self.next_connection_sequence.saturating_add(1);
                let side_effect_started = true;
                self.connections.insert(id.clone(), adapter);
                (vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "connection.open",
                    "success": true,
                    "connection_id": id,
                    "engine": endpoint.engine
                })], side_effect_started)
            }
            Err(err) => (vec![json!({
                "event": "result",
                "request_id": request.request_id,
                "command": "connection.open",
                "success": false,
                "engine": endpoint.engine,
                "message": redact_endpoint_secret(&err, &endpoint)
            })], false),
        }
    }

    fn connection_close(&mut self, request: &Request) -> (Vec<Value>, bool) {
        let connection_id = request
            .payload
            .get("connection_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let side_effect_started = self.connections.contains_key(connection_id);
        let removed = self.connections.remove(connection_id).is_some();
        (vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.close",
            "success": true,
            "closed": removed,
            "connection_id": connection_id
        })], side_effect_started)
    }

    fn query_execute(&mut self, request: &Request) -> (Vec<Value>, bool) {
        if let Some(connection_id) = request.payload.get("connection_id").and_then(Value::as_str) {
            let sql = request
                .payload
                .get("sql")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if sql.is_empty() {
                return (vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": "query.execute requires sql"
                })], false);
            }
            let Some(adapter) = self.connections.get_mut(connection_id) else {
                return (vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": format!("unknown connection_id: {connection_id}")
                })], false);
            };
            let params = query_params(&request.payload);
            let bound_sql = bind_query_params(sql, &params);
            let side_effect_started = query_may_mutate(&bound_sql);
            let events = match execute_query_adapter(adapter, &bound_sql) {
                Ok(result) => query_result_events(request, result),
                Err(err) => vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })],
            };
            return (events, side_effect_started);
        }
        query_execute(request)
    }
}

impl Default for CoreService {
    fn default() -> Self {
        Self::new()
    }
}

pub fn handle_line(line: &str) -> Vec<Value> {
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request(request),
        Err(err) => vec![protocol_error_event(
            None,
            "invalid_request_json",
            format!("invalid request JSON: {err}"),
        )],
    }
}

pub fn handle_line_streaming<F, R>(line: &str, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    match serde_json::from_str::<Request>(line) {
        Ok(request) => handle_request_streaming(request, emit),
        Err(err) => emit(protocol_error_event(
            None,
            "invalid_request_json",
            format!("invalid request JSON: {err}"),
            ))
            .into_protocol_emit_result(),
    }
}

pub fn handle_request(request: Request) -> Vec<Value> {
    let mut events = Vec::new();
    let _ = handle_request_streaming(request, |event| events.push(event));
    events
}

pub fn handle_request_streaming<F, R>(request: Request, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let request_id = request.request_id.clone();
    let command = request.command.clone();
    let mut failed = false;
    handle_request_streaming_unchecked(request, |event| {
        if failed {
            return Err(ProtocolEmitError::io("protocol emitter is fused"));
        }
        let result = emit(normalize_protocol_event(event, &request_id, &command))
            .into_protocol_emit_result();
        if result.is_err() {
            failed = true;
        }
        result
    })
}

fn handle_request_streaming_unchecked<F, R>(
    request: Request,
    mut emit: F,
) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    match request.command.as_str() {
        "service.hello" => emit_all_events(service_hello(&request), emit),
        "service.shutdown" => emit_all_events(service_shutdown(&request), emit),
        "connection.test" => emit_all_events(connection_test(&request), emit),
        "connection.open" => emit_all_events(connection_open(&request), emit),
        "connection.close" => emit_all_events(connection_close(&request), emit),
        "schema.list" => emit_all_events(schema_list(&request), emit),
        "schema.inspect" => emit_all_events(alias_events(&request, "inspect"), emit),
        "schema.diff" => emit_all_events(schema_diff(&request), emit),
        "query.execute" => {
            let (events, side_effect_started) = query_execute(&request);
            emit_operation_events(events, side_effect_started, emit)
        }
        "query.cancel" => emit_all_events(query_cancel(&request), emit),
        "dump.run" => dump_run_streaming(&request, emit),
        "dump.import" => dump_import_streaming(&request, emit),
        "migration.plan" => emit_all_events(alias_events(&request, "plan"), emit),
        "migration.verify" => emit_all_events(alias_events(&request, "verify"), emit),
        "migration.resume" => emit_all_events(alias_events(&request, "resume"), emit),
        "migration.cleanup" => {
            let alias = Request {
                command: "cleanup".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            cleanup_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            })
        }
        "migration.run" => {
            let alias = Request {
                command: "migrate".to_string(),
                request_id: request.request_id.clone(),
                payload: request.payload.clone(),
            };
            let command = request.command.clone();
            migrate_streaming(&alias, |event| {
                emit(rewrite_result_command(event, &command))
            })
        }
        "oneclick.run" => oneclick_run_streaming(&request, emit),
        "oneclick.preflight" => emit_all_events(oneclick_preflight(&request), emit),
        "oneclick.analyze" => emit_all_events(oneclick_analyze(&request), emit),
        "oneclick.plan" => emit_all_events(oneclick_plan(&request), emit),
        "oneclick.recommend" => emit_all_events(oneclick_recommend(&request), emit),
        "oneclick.derive_charset_contracts" => {
            emit_all_events(oneclick_derive_charset_contracts(&request), emit)
        }
        "oneclick.apply_fixes"
            if request
                .payload
                .get("dry_run")
                .and_then(Value::as_bool)
                .unwrap_or(true) =>
        {
            emit_all_events(oneclick_legacy_preview_disabled(&request), emit)
        }
        "oneclick.apply_fixes" => emit_all_events(oneclick_apply_fixes(&request), emit),
        "oneclick.validate" => emit_all_events(oneclick_validate(&request), emit),
        "oneclick.report" => emit_all_events(oneclick_report(&request), emit),
        "job.cancel" => emit_all_events(job_cancel(&request), emit),
        "inspect" => emit_all_events(inspect(&request), emit),
        "preflight" => preflight_streaming(&request, emit),
        "readiness" => emit_all_events(readiness(&request), emit),
        "guide" => emit_all_events(guide(&request), emit),
        "plan" => emit_all_events(plan(&request), emit),
        "migrate" => migrate_streaming(&request, emit),
        "verify" => emit_all_events(verify(&request), emit),
        "resume" => emit_all_events(resume(&request), emit),
        "cleanup" => cleanup_streaming(&request, emit),
        other => emit(json!({
            "event": "error",
            "request_id": request.request_id,
            "message": format!("unknown command: {other}")
        }))
        .into_protocol_emit_result(),
    }
}

pub fn protocol_error_event(
    request_id: Option<String>,
    code: impl AsRef<str>,
    message: impl AsRef<str>,
) -> Value {
    let request_id = request_id
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| PROTOCOL_ERROR_REQUEST_ID.to_string());
    json!({
        "event": "error",
        "request_id": request_id,
        "command": Value::Null,
        "code": nonempty_or_default(code.as_ref(), "protocol_error"),
        "message": nonempty_or_default(message.as_ref(), "unknown protocol error")
    })
}

fn normalize_protocol_event(event: Value, request_id: &Option<String>, command: &str) -> Value {
    let Value::Object(mut fields) = event else {
        let mut error = protocol_error_event(
            request_id.clone(),
            "invalid_protocol_event",
            "protocol handler emitted a non-object event",
        );
        error["command"] = Value::String(command.to_string());
        return error;
    };

    if fields.get("event") != Some(&Value::String("error".to_string())) {
        return Value::Object(fields);
    }

    let code = fields
        .get("code")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("protocol_error")
        .to_string();
    let message = fields
        .get("message")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("unknown protocol error")
        .to_string();
    fields.insert("request_id".to_string(), json!(request_id));
    fields.insert("command".to_string(), Value::String(command.to_string()));
    fields.insert("code".to_string(), Value::String(code));
    fields.insert("message".to_string(), Value::String(message));
    Value::Object(fields)
}

fn nonempty_or_default(value: &str, default: &str) -> String {
    if value.trim().is_empty() {
        default.to_string()
    } else {
        value.to_string()
    }
}

fn emit_all_events<F, R>(events: Vec<Value>, mut emit: F) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    for event in events {
        emit(event).into_protocol_emit_result()?;
    }
    Ok(())
}

fn emit_operation_events<F, R>(
    events: Vec<Value>,
    side_effect_started: bool,
    emit: F,
) -> ProtocolEmitResult
where
    F: FnMut(Value) -> R,
    R: IntoProtocolEmitResult,
{
    let result = emit_all_events(events, emit);
    if side_effect_started {
        result.map_err(ProtocolEmitError::after_side_effect)
    } else {
        result
    }
}

fn alias_events(request: &Request, legacy_command: &str) -> Vec<Value> {
    let legacy = Request {
        command: legacy_command.to_string(),
        request_id: request.request_id.clone(),
        payload: request.payload.clone(),
    };
    handle_request(legacy)
        .into_iter()
        .map(|event| rewrite_result_command(event, &request.command))
        .collect()
}

fn rewrite_result_command(mut event: Value, command: &str) -> Value {
    if event.get("event") == Some(&json!("result")) {
        if let Value::Object(object) = &mut event {
            object.insert("command".to_string(), Value::String(command.to_string()));
        }
    }
    event
}

fn service_hello(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.hello",
        "success": true,
        "service": "tunnelforge-core",
        "protocol_version": PROTOCOL_VERSION,
        "process_version": PROCESS_VERSION,
        "process_capabilities": PROCESS_CAPABILITIES,
        "max_jsonl_frame_bytes": MAX_JSONL_FRAME_BYTES,
        "max_assembled_event_bytes": MAX_ASSEMBLED_EVENT_BYTES,
        "max_assembled_event_chunks": MAX_ASSEMBLED_EVENT_CHUNKS,
        "max_assembled_event_nodes": MAX_ASSEMBLED_EVENT_NODES,
        "max_assembled_event_depth": MAX_ASSEMBLED_EVENT_DEPTH,
        "oneclick_plan_version": ONECLICK_PLAN_VERSION,
        "oneclick_approval_version": ONECLICK_APPROVAL_VERSION,
        "oneclick_profile_version": ONECLICK_PROFILE_VERSION,
        "oneclick_action_facts_version": ACTION_FACTS_VERSION,
        "oneclick_exact_plan_enabled": ONECLICK_EXACT_PLAN_ENABLED,
        "oneclick_strong_fence_proven": ONECLICK_STRONG_FENCE_PROVEN,
        "capabilities": PUBLIC_COMMANDS
    })]
}

fn service_shutdown(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "service.shutdown",
        "success": true
    })]
}

fn connection_test(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": true,
            "engine": endpoint.engine,
            "message": "connection successful"
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.test",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_open(request: &Request) -> Vec<Value> {
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })]
        }
    };

    match LiveAdapter::connect(&endpoint) {
        Ok(_) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": true,
            "connection_id": connection_id(&endpoint),
            "engine": endpoint.engine
        })],
        Err(err) => vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "connection.open",
            "success": false,
            "engine": endpoint.engine,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    }
}

fn connection_close(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "connection.close",
        "success": true,
        "connection_id": request.payload.get("connection_id").cloned().unwrap_or(Value::Null)
    })]
}

fn schema_list(request: &Request) -> Vec<Value> {
    if let Ok(endpoint) = request_endpoint(request) {
        match inspect_live(&endpoint) {
            Ok(result) => {
                let tables: Vec<String> = result
                    .schema
                    .tables
                    .iter()
                    .map(|table| table.name.clone())
                    .collect();
                return vec![json!({
                    "event": "result",
                    "request_id": request.request_id,
                    "command": "schema.list",
                    "success": true,
                    "engine": endpoint.engine,
                    "schema": endpoint_schema(&endpoint),
                    "tables": tables
                })];
            }
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": redact_endpoint_secret(&err, &endpoint)
                })]
            }
        }
    }

    let schema = parse_schema(&request.payload["schema"]).unwrap_or_default();
    let tables: Vec<String> = schema
        .tables
        .iter()
        .map(|table| table.name.clone())
        .collect();
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.list",
        "success": true,
        "tables": tables
    })]
}

fn schema_diff(request: &Request) -> Vec<Value> {
    let source_schema = if request.payload.get("source_schema").is_some() {
        parse_schema(&request.payload["source_schema"]).unwrap_or_default()
    } else if request.payload.get("source").is_some() {
        match request
            .payload
            .get("source")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };
    let target_schema = if request.payload.get("target_schema").is_some() {
        parse_schema(&request.payload["target_schema"]).unwrap_or_default()
    } else if request.payload.get("target").is_some() {
        match request
            .payload
            .get("target")
            .map(endpoint_from_value)
            .transpose()
            .and_then(|endpoint| {
                endpoint
                    .map(|endpoint| inspect_live(&endpoint).map(|result| result.schema))
                    .unwrap_or_else(|| Ok(NormalizedSchema::default()))
            }) {
            Ok(schema) => schema,
            Err(err) => {
                return vec![json!({
                    "event": "error",
                    "request_id": request.request_id,
                    "message": err
                })]
            }
        }
    } else {
        NormalizedSchema::default()
    };

    let differences = normalized_schema_diff(&source_schema, &target_schema);
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "schema.diff",
        "success": differences.is_empty(),
        "differences": differences
    })]
}

fn query_execute(request: &Request) -> (Vec<Value>, bool) {
    query_execute_with(request, execute_query_live)
}

fn query_execute_with<F>(request: &Request, execute: F) -> (Vec<Value>, bool)
where
    F: FnOnce(&Endpoint, &str) -> Result<QueryExecutionResult, String>,
{
    if let Some(rows) = request.payload.get("rows") {
        let columns = request
            .payload
            .get("columns")
            .cloned()
            .unwrap_or_else(|| json!(memory_test_columns_from_rows(rows)));
        return (vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": rows,
            "columns": columns,
            "rows_affected": 0
        })], false);
    }

    let sql = request
        .payload
        .get("sql")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if sql.is_empty() {
        return (vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": "query.execute requires sql"
        })], false);
    }
    let endpoint = match request_endpoint(request) {
        Ok(endpoint) => endpoint,
        Err(err) => {
            return (vec![json!({
                "event": "error",
                "request_id": request.request_id,
                "message": err
            })], false)
        }
    };

    let params = query_params(&request.payload);
    let bound_sql = bind_query_params(sql, &params);
    let side_effect_started = query_may_mutate(&bound_sql);
    let events = match execute(&endpoint, &bound_sql) {
        Ok(result) => query_result_events(request, result),
        Err(err) => vec![json!({
            "event": "error",
            "request_id": request.request_id,
            "message": redact_endpoint_secret(&err, &endpoint)
        })],
    };
    (events, side_effect_started)
}

fn memory_test_columns_from_rows(rows: &Value) -> Vec<String> {
    rows.as_array()
        .and_then(|items| items.first())
        .and_then(Value::as_object)
        .map(|object| object.keys().cloned().collect())
        .unwrap_or_default()
}

pub(crate) struct QueryExecutionResult {
    pub(crate) rows: Vec<Value>,
    pub(crate) columns: Vec<String>,
    pub(crate) rows_affected: u64,
}

fn query_result_events(request: &Request, result: QueryExecutionResult) -> Vec<Value> {
    let stream_rows = request
        .payload
        .get("stream_rows")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !stream_rows {
        return vec![json!({
            "event": "result",
            "request_id": request.request_id,
            "command": "query.execute",
            "success": true,
            "rows": result.rows,
            "columns": result.columns,
            "rows_affected": result.rows_affected
        })];
    }

    let batch_size = request
        .payload
        .get("row_batch_size")
        .and_then(Value::as_u64)
        .unwrap_or(500)
        .max(1) as usize;
    let total = result.rows.len();
    let mut events = vec![json!({
        "event": "columns",
        "request_id": request.request_id,
        "command": "query.execute",
        "columns": result.columns.clone()
    })];
    for (index, chunk) in result.rows.chunks(batch_size).enumerate() {
        events.push(json!({
            "event": "row_batch",
            "request_id": request.request_id,
            "command": "query.execute",
            "batch_index": index,
            "rows": chunk,
            "total": total
        }));
    }
    events.push(json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.execute",
        "success": true,
        "rows": [],
        "columns": result.columns,
        "rows_streamed": total,
        "rows_affected": result.rows_affected
    }));
    events
}

fn query_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "query.cancel",
        "success": true,
        "cancelled": false,
        "message": "No asynchronous query is registered for this JSONL worker",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}

fn job_cancel(request: &Request) -> Vec<Value> {
    vec![json!({
        "event": "result",
        "request_id": request.request_id,
        "command": "job.cancel",
        "success": true,
        "cancelled": false,
        "message": "UI workers cancel long-running Rust jobs by terminating the isolated core process",
        "job_id": request.payload.get("job_id").cloned().unwrap_or(Value::Null)
    })]
}

#[cfg(test)]
mod tests {
    use super::*;

    use serde_json::{json, Value};

    use crate::adapters::test_support::schema;

    fn assert_error_code(events: &[Value], expected: &str) {
        assert_eq!(events.len(), 1, "mutation gate must be the first event");
        assert_eq!(events[0]["event"], "error");
        assert_eq!(events[0]["code"], expected);
        assert!(events[0]["message"].as_str().is_some());
    }

    fn assert_protocol_error_envelope(events: &[Value], request_id: Value) {
        assert_eq!(events.len(), 1, "outer paths must emit one error event");
        let event = &events[0];
        assert_eq!(event["event"], "error");
        assert_eq!(event["request_id"], request_id);
        assert!(matches!(event["code"].as_str(), Some(value) if !value.is_empty()));
        assert!(matches!(event["message"].as_str(), Some(value) if !value.is_empty()));
    }

    fn valid_oneclick_apply_payload() -> Value {
        json!({
            "connection": {
                "engine": "mysql",
                "host": "127.0.0.1",
                "port": 3306,
                "user": "app",
                "password": "secret",
                "database": "app",
                "schema": "app"
            },
            "schema": "app",
            "dry_run": false,
            "backup_confirmed": true,
            "approval": {
                "approval_version": 1,
                "plan_version": 1,
                "target_identity": {
                    "engine": "mysql",
                    "route": {"host": "127.0.0.1", "port": 3306},
                    "server_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "authenticated_user": "app@localhost",
                    "schema": "app"
                },
                "remediation_profile": {
                    "profile_version": 1,
                    "profile_id": "mysql-utf8mb4-0900-v1",
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci"
                },
                "snapshot_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "plan_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            }
        })
    }

    #[test]
    fn service_hello_advertises_core_protocol() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-1".to_string()),
            payload: json!({}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["request_id"], "hello-1");
        assert_eq!(result["command"], "service.hello");
        assert_eq!(result["service"], "tunnelforge-core");
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("migration.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.run")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("dump.import")));
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.plan")));
        assert!(!result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.run")));
        assert!(!result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.recommend")));
        assert!(!result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.derive_charset_contracts")));
        assert!(!result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("oneclick.apply_fixes")));
        assert_eq!(result["oneclick_plan_version"], 1);
        assert_eq!(result["oneclick_approval_version"], 1);
        assert_eq!(result["oneclick_profile_version"], 1);
        assert_eq!(result["oneclick_action_facts_version"], 1);
        assert_eq!(result["oneclick_exact_plan_enabled"], false);
        assert_eq!(result["oneclick_strong_fence_proven"], false);
    }

    #[test]
    fn service_hello_advertises_exact_process_contract() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-process-1".to_string()),
            payload: json!({}),
        })
        .into_iter()
        .next()
        .unwrap();

        assert_eq!(result["protocol_version"], 1);
        assert_eq!(result["process_version"], 1);
        assert_eq!(
            result["process_capabilities"],
            json!([
                "request.deadline",
                "request.strict_id",
                "process.generation",
                "mutation.outcome_indeterminate"
            ])
        );
        assert_eq!(
            result["capabilities"],
            json!([
                "connection.open",
                "connection.close",
                "connection.test",
                "schema.list",
                "schema.inspect",
                "schema.diff",
                "query.execute",
                "query.cancel",
                "dump.run",
                "dump.import",
                "migration.plan",
                "migration.run",
                "migration.verify",
                "migration.resume",
                "migration.cleanup",
                "oneclick.preflight",
                "oneclick.analyze",
                "oneclick.plan",
                "oneclick.validate",
                "oneclick.report",
                "job.cancel",
                "service.shutdown"
            ])
        );
        assert!(result["capabilities"]
            .as_array()
            .unwrap()
            .contains(&json!("migration.cleanup")));
    }

    #[test]
    fn oneclick_plan_protocol_rejects_prohibited_payload_without_db_work() {
        for key in [
            "issues",
            "charset_contracts",
            "target_charset",
            "target_collation",
            "actions",
            "steps",
            "profile",
            "remediation_profile",
            "approval",
            "dry_run",
            "backup_confirmed",
            "unknown",
        ] {
            let mut payload = json!({
                "connection": {
                    "engine": "mysql",
                    "host": "invalid.invalid",
                    "port": 3306,
                    "user": "app",
                    "password": "must-not-leak"
                },
                "schema": "app"
            });
            payload[key] = json!(true);
            let events = handle_request(Request {
                command: "oneclick.plan".to_string(),
                request_id: Some(format!("plan-prohibited-{key}")),
                payload,
            });

            assert_error_code(&events, "oneclick_plan_payload_prohibited");
            assert!(!serde_json::to_string(&events).unwrap().contains("must-not-leak"));
        }
    }

    #[test]
    fn oneclick_plan_protocol_legacy_preview_matrix_fails_closed() {
        let cases = [
            ("oneclick.run", json!({})),
            ("oneclick.run", json!({"dry_run": false})),
            ("oneclick.recommend", json!({"issues": []})),
            (
                "oneclick.derive_charset_contracts",
                json!({"table_facts": [], "fk_facts": []}),
            ),
            ("oneclick.apply_fixes", json!({})),
            ("oneclick.apply_fixes", json!({"dry_run": true})),
        ];

        for (command, payload) in cases {
            let events = handle_request(Request {
                command: command.to_string(),
                request_id: Some(format!("legacy-{command}")),
                payload,
            });
            assert_error_code(&events, "oneclick_legacy_preview_disabled");
        }

        let apply_events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("legacy-apply-real".to_string()),
            payload: json!({"dry_run": false}),
        });
        assert_error_code(&apply_events, "oneclick_apply_disabled");
        assert!(!PUBLIC_COMMANDS.contains(&"oneclick.apply_fixes"));
    }

    #[test]
    fn oneclick_apply_raw_parser_rejects_backup_approval_and_profile_substitution() {
        let cases = [
            ("backup-missing", vec!["backup_confirmed"], json!(null), "oneclick_backup_required"),
            ("backup-false", Vec::new(), json!({"backup_confirmed": false}), "oneclick_backup_required"),
            ("backup-malformed", Vec::new(), json!({"backup_confirmed": "yes"}), "oneclick_backup_required"),
            ("approval-missing", vec!["approval"], json!(null), "oneclick_approval_required"),
            ("approval-malformed", Vec::new(), json!({"approval": []}), "oneclick_approval_required"),
            ("approval-version", Vec::new(), json!({"approval": {"approval_version": 2}}), "oneclick_approval_version_unsupported"),
            ("profile-missing", Vec::new(), json!({"approval": {"remediation_profile": null}}), "oneclick_profile_required"),
            ("profile-version", Vec::new(), json!({"approval": {"remediation_profile": {"profile_version": 2}}}), "oneclick_profile_unsupported"),
            ("profile-substitution", Vec::new(), json!({"approval": {"remediation_profile": {"profile_id": "client-selected"}}}), "oneclick_profile_substitution"),
        ];

        for (name, removed, overlay, expected) in cases {
            let mut payload = valid_oneclick_apply_payload();
            let root = payload.as_object_mut().unwrap();
            for key in removed {
                root.remove(key);
            }
            if let Some(overlay) = overlay.as_object() {
                for (key, value) in overlay {
                    if key == "approval" && value.is_object() {
                        for (approval_key, approval_value) in value.as_object().unwrap() {
                            if approval_key == "remediation_profile" && approval_value.is_object() {
                                let profile = root["approval"]["remediation_profile"]
                                    .as_object_mut()
                                    .unwrap();
                                for (profile_key, profile_value) in approval_value.as_object().unwrap() {
                                    profile.insert(profile_key.clone(), profile_value.clone());
                                }
                            } else {
                                root["approval"][approval_key] = approval_value.clone();
                            }
                        }
                    } else {
                        root.insert(key.clone(), value.clone());
                    }
                }
            }
            let error = parse_oneclick_apply_request(&Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: Some(name.to_string()),
                payload,
            })
            .expect_err(name);
            assert_eq!(error.code(), expected, "{name}");
            assert!(error.applied_ordinals().is_empty(), "{name}");
        }
    }

    #[test]
    fn oneclick_apply_raw_parser_rejects_prohibited_fields_and_schema_mismatches() {
        for key in [
            "issues",
            "charset_contracts",
            "target_charset",
            "target_collation",
            "actions",
            "steps",
            "profile",
            "remediation_profile",
            "unknown",
        ] {
            let mut payload = valid_oneclick_apply_payload();
            payload[key] = json!(true);
            let error = parse_oneclick_apply_request(&Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: None,
                payload,
            })
            .expect_err(key);
            assert_eq!(error.code(), "oneclick_apply_payload_prohibited", "{key}");
        }

        let mut payload = valid_oneclick_apply_payload();
        payload["connection"]["endpoint_override"] = json!(true);
        let error = parse_oneclick_apply_request(&Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: None,
            payload,
        })
        .expect_err("nested connection override");
        assert_eq!(error.code(), "oneclick_apply_payload_prohibited");

        let mut payload = valid_oneclick_apply_payload();
        payload["approval"]["actions"] = json!([]);
        let error = parse_oneclick_apply_request(&Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: None,
            payload,
        })
        .expect_err("nested approval actions");
        assert_eq!(error.code(), "oneclick_apply_payload_prohibited");

        for path in ["database", "schema"] {
            let mut payload = valid_oneclick_apply_payload();
            payload["connection"][path] = json!("other");
            let error = parse_oneclick_apply_request(&Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: None,
                payload,
            })
            .expect_err(path);
            assert_eq!(error.code(), "oneclick_schema_mismatch", "{path}");
        }

        let mut payload = valid_oneclick_apply_payload();
        payload["approval"]["target_identity"]["schema"] = json!("other");
        let error = parse_oneclick_apply_request(&Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: None,
            payload,
        })
        .expect_err("approval schema mismatch");
        assert_eq!(error.code(), "oneclick_schema_mismatch");
    }

    #[test]
    fn oneclick_apply_raw_parser_rejects_missing_and_malformed_versions() {
        for (key, replacement) in [
            ("approval_version", None),
            ("approval_version", Some(json!("1"))),
            ("plan_version", None),
            ("plan_version", Some(json!(2))),
        ] {
            let mut payload = valid_oneclick_apply_payload();
            let approval = payload["approval"].as_object_mut().unwrap();
            match replacement {
                Some(value) => {
                    approval.insert(key.to_string(), value);
                }
                None => {
                    approval.remove(key);
                }
            }
            let error = parse_oneclick_apply_request(&Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: None,
                payload,
            })
            .expect_err(key);
            assert_eq!(error.code(), "oneclick_approval_version_unsupported");
        }
    }

    #[test]
    fn oneclick_apply_gate_reaches_factory_only_when_both_proofs_are_true() {
        for (exact_plan, strong_fence, expected_calls) in [
            (false, false, 0),
            (true, false, 0),
            (false, true, 0),
            (true, true, 1),
        ] {
            let request = Request {
                command: "oneclick.apply_fixes".to_string(),
                request_id: Some(format!("gate-{exact_plan}-{strong_fence}")),
                payload: valid_oneclick_apply_payload(),
            };
            let calls = Cell::new(0usize);
            let events = oneclick_apply_with_session_factory(
                &request,
                exact_plan,
                strong_fence,
                |_validated| {
                    calls.set(calls.get() + 1);
                    vec![json!({
                        "event": "result",
                        "request_id": request.request_id,
                        "command": request.command,
                        "success": true,
                        "applied_ordinals": []
                    })]
                },
            );

            assert_eq!(calls.get(), expected_calls);
            if expected_calls == 0 {
                assert_error_code(&events, "oneclick_apply_disabled");
                assert_eq!(events[0]["applied_ordinals"], json!([]));
            } else {
                assert_eq!(events[0]["success"], true);
            }
        }
    }

    #[test]
    fn oneclick_apply_public_gate_precedes_raw_parser() {
        let request = Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("gate-before-parser".to_string()),
            payload: json!({"dry_run": false, "backup_confirmed": false}),
        };
        let calls = Cell::new(0usize);

        let events = oneclick_apply_with_session_factory(&request, false, false, |_| {
            calls.set(calls.get() + 1);
            Vec::new()
        });

        assert_error_code(&events, "oneclick_apply_disabled");
        assert_eq!(events[0]["applied_ordinals"], json!([]));
        assert_eq!(calls.get(), 0);
    }

    #[test]
    fn oneclick_apply_legacy_preview_stops_before_session_factory() {
        let mut payload = valid_oneclick_apply_payload();
        payload["dry_run"] = json!(true);
        let request = Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("legacy-preview".to_string()),
            payload,
        };
        let calls = Cell::new(0usize);

        let events = oneclick_apply_with_session_factory(&request, true, true, |_| {
            calls.set(calls.get() + 1);
            Vec::new()
        });

        assert_error_code(&events, "oneclick_legacy_preview_disabled");
        assert_eq!(calls.get(), 0);
    }

    #[test]
    fn service_hello_advertises_exact_max_jsonl_frame_bytes() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-frame-1".to_string()),
            payload: json!({}),
        })
        .into_iter()
        .next()
        .unwrap();

        assert_eq!(result["max_jsonl_frame_bytes"], 1_048_576);
    }

    #[test]
    fn service_hello_advertises_exact_aggregate_limits() {
        let result = handle_request(Request {
            command: "service.hello".to_string(),
            request_id: Some("hello-aggregate-1".to_string()),
            payload: json!({}),
        })
        .pop()
        .unwrap();

        assert_eq!(result["max_assembled_event_bytes"], 64 * 1024 * 1024);
        assert_eq!(result["max_assembled_event_chunks"], 4_096);
        assert_eq!(result["max_assembled_event_nodes"], 65_536);
        assert_eq!(result["max_assembled_event_depth"], 128);
    }

    #[test]
    fn public_command_capabilities_include_migration_cleanup() {
        let events = handle_request(Request {
            command: "migration.cleanup".to_string(),
            request_id: Some("cleanup-capability-1".to_string()),
            payload: json!({
                "target_engine": "postgresql",
                "schema": {"tables": []}
            }),
        });

        let result = events
            .iter()
            .find(|event| event["event"] == "result")
            .unwrap();
        assert_eq!(result["command"], "migration.cleanup");
    }

    #[test]
    fn outer_error_paths_preserve_request_id_and_use_protocol_envelope() {
        let unknown_request = || Request {
            command: "unknown.command".to_string(),
            request_id: Some("outer-request-1".to_string()),
            payload: json!({}),
        };

        assert_protocol_error_envelope(
            &handle_request(unknown_request()),
            json!("outer-request-1"),
        );

        let mut streaming_events = Vec::new();
        let _ = handle_request_streaming(unknown_request(), |event| {
            streaming_events.push(event)
        });
        assert_protocol_error_envelope(&streaming_events, json!("outer-request-1"));

        let mut service = CoreService::new();
        let mut stateful_events = Vec::new();
        let _ = service.handle_request_streaming(unknown_request(), |event| {
            stateful_events.push(event)
        });
        assert_protocol_error_envelope(&stateful_events, json!("outer-request-1"));

        assert_protocol_error_envelope(
            &handle_line("{"),
            json!("protocol-invalid-request-id"),
        );

        let mut line_streaming_events = Vec::new();
        let _ = handle_line_streaming("{", |event| line_streaming_events.push(event));
        assert_protocol_error_envelope(
            &line_streaming_events,
            json!("protocol-invalid-request-id"),
        );
    }

    #[test]
    fn protocol_error_normalization_replaces_missing_empty_and_non_string_fields() {
        for event in [
            json!({"event": "error"}),
            json!({"event": "error", "code": "", "message": ""}),
            json!({"event": "error", "code": 7, "message": false}),
        ] {
            let event = normalize_protocol_event(
                event,
                &Some("normalized-1".to_string()),
                "schema.list",
            );
            assert_eq!(event["request_id"], "normalized-1");
            assert_eq!(event["command"], "schema.list");
            assert!(matches!(event["code"].as_str(), Some(value) if !value.is_empty()));
            assert!(matches!(event["message"].as_str(), Some(value) if !value.is_empty()));
        }
    }

    #[test]
    fn oneclick_recommend_contract_returns_manual_steps() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-1".to_string()),
            payload: json!({
                "issues": [{
                    "severity": "warning",
                    "location": "backup",
                    "message": "Backup confirmation was not provided.",
                    "suggestion": "Confirm backup.",
                    "blocking": false
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_recommend_classifies_deprecated_engine_as_auto_fixable() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-auto-1".to_string()),
            payload: json!({
                "schema": "app",
                "issues": [{
                    "issue_type": "deprecated_engine",
                    "severity": "warning",
                    "location": "app.legacy_table",
                    "table_name": "legacy_table",
                    "message": "Deprecated storage engine detected.",
                    "suggestion": "Convert the table to InnoDB.",
                    "blocking": false
                }, {
                    "issue_type": "zerofill_usage",
                    "severity": "warning",
                    "location": "app.orders.code",
                    "table_name": "orders",
                    "column_name": "code",
                    "message": "ZEROFILL usage detected.",
                    "suggestion": "Handle display padding in the application.",
                    "blocking": false
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_recommend_gates_charset_auto_fix_on_complete_contract() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-charset-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }],
                "charset_contracts": [{
                    "issue_index": 0,
                    "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                    "target_charset": "utf8mb4",
                    "target_collation": "utf8mb4_0900_ai_ci",
                    "rollback_sql": [
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                        "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                    ]
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_recommend_keeps_charset_manual_without_complete_contract() {
        let events = handle_request(Request {
            command: "oneclick.recommend".to_string(),
            request_id: Some("oneclick-rec-charset-manual-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_derive_charset_contracts_command_returns_contracts_from_safe_facts() {
        let events = handle_request(Request {
            command: "oneclick.derive_charset_contracts".to_string(),
            request_id: Some("derive-charset-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "target_charset": "utf8mb4",
                "target_collation": "utf8mb4_0900_ai_ci",
                "issues": [{
                    "issue_type": "charset_issue",
                    "severity": "warning",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "message": "Table uses a legacy charset.",
                    "suggestion": "Convert table charset/collation after FK-safe review.",
                    "blocking": false
                }],
                "table_facts": [{
                    "table": "tf_oneclick_parent",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci"
                }, {
                    "table": "tf_oneclick_child",
                    "charset": "utf8mb3",
                    "collation": "utf8mb3_general_ci"
                }],
                "foreign_key_facts": [{
                    "table": "tf_oneclick_child",
                    "referenced_table": "tf_oneclick_parent"
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_apply_fixes_defaults_to_dry_run() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-1".to_string()),
            payload: json!({"steps": [{"location": "backup"}]}),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_run_non_dry_run_fails_closed_before_endpoint_or_sql() {
        let events = handle_request(Request {
            command: "oneclick.run".to_string(),
            request_id: Some("oneclick-run-disabled-1".to_string()),
            payload: json!({"dry_run": false, "backup_confirmed": true}),
        });

        assert_error_code(&events, "oneclick_legacy_preview_disabled");
        assert_eq!(events[0]["request_id"], "oneclick-run-disabled-1");
    }

    #[test]
    fn oneclick_run_preserves_explicit_and_default_dry_run_preflight() {
        for (request_id, payload) in [
            ("oneclick-run-dry-1", json!({"dry_run": true})),
            ("oneclick-run-default-dry-1", json!({})),
        ] {
            let events = handle_request(Request {
                command: "oneclick.run".to_string(),
                request_id: Some(request_id.to_string()),
                payload,
            });

            assert_error_code(&events, "oneclick_legacy_preview_disabled");
            assert_eq!(events[0]["request_id"], request_id);
        }
    }

    #[test]
    fn oneclick_apply_fixes_dry_run_previews_charset_plan_without_executing_sql() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-charset-dry-1".to_string()),
            payload: json!({
                "schema": "tf_oneclick_charset",
                "dry_run": true,
                "steps": [{
                    "issue_type": "charset_issue",
                    "location": "tf_oneclick_charset.tf_oneclick_parent",
                    "table_name": "tf_oneclick_parent",
                    "selected_option": {
                        "strategy": "charset_collation_fk_safe",
                        "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
                        "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
                        "target_charset": "utf8mb4",
                        "target_collation": "utf8mb4_0900_ai_ci",
                        "sql": [
                            "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                            "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
                        ],
                        "rollback_sql": [
                            "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                            "ALTER TABLE `tf_oneclick_charset`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;"
                        ]
                    }
                }]
            }),
        });
        assert_error_code(&events, "oneclick_legacy_preview_disabled");
    }

    #[test]
    fn oneclick_apply_fixes_non_dry_run_fails_closed_before_endpoint_or_sql() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-disabled-1".to_string()),
            payload: valid_oneclick_apply_payload(),
        });

        assert_error_code(&events, "oneclick_apply_disabled");
        assert_eq!(events[0]["request_id"], "oneclick-apply-disabled-1");
    }

    #[test]
    fn oneclick_apply_fixes_non_dry_run_blocks_actions_before_endpoint() {
        let events = handle_request(Request {
            command: "oneclick.apply_fixes".to_string(),
            request_id: Some("oneclick-apply-real-1".to_string()),
            payload: json!({
                "schema": "app",
                "dry_run": false,
                "steps": [{
                    "issue_type": "deprecated_engine",
                    "location": "app.legacy_table",
                    "selected_option": {
                        "strategy": "engine_innodb",
                        "sql_template": "ALTER TABLE `app`.`legacy_table` ENGINE=InnoDB;"
                    }
                }]
            }),
        });

        assert_error_code(&events, "oneclick_apply_disabled");
        assert_eq!(events[0]["request_id"], "oneclick-apply-real-1");
    }

    #[test]
    fn schema_diff_reports_table_column_and_type_differences() {
        let result = handle_request(Request {
            command: "schema.diff".to_string(),
            request_id: None,
            payload: json!({
                "source_schema": schema(),
                "target_schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "bigint", "nullable": false}]
                    }, {
                        "name": "audit",
                        "columns": []
                    }]
                }
            }),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        let differences = result["differences"].as_array().unwrap();
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "missing_column" && diff["column"] == "name"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "extra_table" && diff["table"] == "audit"));
        assert!(differences
            .iter()
            .any(|diff| diff["kind"] == "type_mismatch" && diff["column"] == "id"));
    }

    #[test]
    fn query_execute_accepts_memory_rows_for_contract_tests() {
        let result = handle_request(Request {
            command: "query.execute".to_string(),
            request_id: None,
            payload: json!({"rows": [{"id": 1, "name": "alpha"}]}),
        })
        .into_iter()
        .find(|event| event.get("event") == Some(&json!("result")))
        .unwrap();

        assert_eq!(result["command"], "query.execute");
        assert_eq!(result["rows"][0]["name"], "alpha");
    }

    #[test]
    fn query_result_streams_row_batches_when_requested() {
        let events = query_result_events(
            &Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({"stream_rows": true, "row_batch_size": 1}),
            },
            QueryExecutionResult {
                rows: vec![json!({"id": 1}), json!({"id": 2})],
                columns: vec!["id".to_string()],
                rows_affected: 0,
            },
        );

        assert_eq!(events[0]["event"], "columns");
        assert_eq!(events[0]["columns"], json!(["id"]));
        assert_eq!(events[1]["event"], "row_batch");
        assert_eq!(events[1]["rows"][0]["id"], 1);
        assert_eq!(events[2]["event"], "row_batch");
        assert_eq!(events[3]["event"], "result");
        assert_eq!(events[3]["rows_streamed"], 2);
        assert_eq!(events[3]["columns"], json!(["id"]));
    }

    #[test]
    fn query_result_includes_non_row_rows_affected() {
        let events = query_result_events(
            &Request {
                command: "query.execute".to_string(),
                request_id: Some("query-1".to_string()),
                payload: json!({}),
            },
            QueryExecutionResult {
                rows: Vec::new(),
                columns: Vec::new(),
                rows_affected: 7,
            },
        );

        assert_eq!(events[0]["event"], "result");
        assert_eq!(events[0]["rows_affected"], 7);
        assert_eq!(events[0]["rows"], json!([]));
        assert_eq!(events[0]["columns"], json!([]));
    }

    #[test]
    fn migrate_command_emits_chunk_checkpoints_before_result() {
        let events = handle_request(Request {
            command: "migrate".to_string(),
            request_id: Some("req-1".to_string()),
            payload: json!({
                "schema": {
                    "tables": [{
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "primary_key": true}]
                    }]
                },
                "execution_options": {"mode": "append", "chunk_size": 1},
                "source_data": {"users": [{"id": 1}, {"id": 2}]},
                "target_data": {}
            }),
        });

        let first_row_progress = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("row_progress")))
            .unwrap();
        let result = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert!(first_row_progress < result);
        assert_eq!(events[first_row_progress]["request_id"], "req-1");
        assert!(events[first_row_progress]["state"].is_object());
    }

    #[test]
    fn preflight_emits_actionable_phase_events_before_result() {
        let events = handle_request(Request {
            command: "preflight".to_string(),
            request_id: Some("preflight-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {"tables": []},
                "options": {"mode": "append"}
            }),
        });

        let phase_messages: Vec<&str> = events
            .iter()
            .filter(|event| event.get("event") == Some(&json!("phase")))
            .filter_map(|event| event.get("message").and_then(Value::as_str))
            .collect();
        let result_index = events
            .iter()
            .position(|event| event.get("event") == Some(&json!("result")))
            .unwrap();
        let last_phase_index = events
            .iter()
            .rposition(|event| event.get("event") == Some(&json!("phase")))
            .unwrap();

        assert!(phase_messages.contains(&"preflight checks started"));
        assert!(phase_messages.contains(&"schema compatibility checks completed"));
        assert!(phase_messages.contains(&"target state checks completed"));
        assert!(phase_messages.contains(&"preflight result ready"));
        assert!(last_phase_index < result_index);
    }

    #[test]
    fn cleanup_command_drops_target_tables_in_reverse_dependency_order() {
        let events = handle_request(Request {
            command: "cleanup".to_string(),
            request_id: Some("cleanup-1".to_string()),
            payload: json!({
                "target_engine": "postgresql",
                "schema": {
                    "tables": [
                        {
                            "name": "parents",
                            "columns": [{"name": "id", "type": "int", "primary_key": true}]
                        },
                        {
                            "name": "children",
                            "columns": [
                                {"name": "id", "type": "int", "primary_key": true},
                                {"name": "parent_id", "type": "int"}
                            ],
                            "foreign_keys": [{
                                "name": "children_parent_id_fk",
                                "columns": ["parent_id"],
                                "referenced_table": "parents",
                                "referenced_columns": ["id"]
                            }]
                        }
                    ]
                }
            }),
        });

        let result = events
            .iter()
            .find(|event| event.get("event") == Some(&json!("result")))
            .unwrap();

        assert_eq!(result["command"], "cleanup");
        assert_eq!(result["success"], true);
        assert_eq!(result["dropped_tables"], json!(["children", "parents"]));
    }

    #[test]
    fn frame_encoder_keeps_direct_jsonl_within_wire_cap() {
        let event = json!({
            "event": "result",
            "request_id": "direct-frame-1",
            "command": "schema.list",
            "success": true,
            "schemas": ["public"]
        });

        let frames = collect_protocol_frames(&event).expect("encode direct frame");

        assert_eq!(frames.len(), 1);
        assert!(frames[0].ends_with(b"\n"));
        assert!(frames[0].len() <= MAX_JSONL_FRAME_BYTES);
        assert_eq!(
            serde_json::from_slice::<Value>(&frames[0]).expect("parse direct frame"),
            event
        );
    }

    #[test]
    fn frame_encoder_chunks_oversized_utf8_at_code_point_boundaries() {
        let value = "🙂한".repeat(180_000);
        let event = json!({
            "event": "result",
            "request_id": "utf8-frame-1",
            "command": "query.execute",
            "success": true,
            "value": value
        });

        let frames = collect_protocol_frames(&event).expect("encode chunked frames");

        assert!(frames.len() > 1);
        let parsed = frames
            .iter()
            .map(|frame| {
                assert!(frame.len() <= MAX_JSONL_FRAME_BYTES);
                assert!(std::str::from_utf8(frame).is_ok());
                serde_json::from_slice::<Value>(frame).expect("parse chunk frame")
            })
            .collect::<Vec<_>>();
        assert!(parsed
            .iter()
            .all(|frame| frame["event"] == "payload_chunk"));

        let mut strings = BTreeMap::<u64, String>::new();
        for frame in &parsed {
            if frame["value_kind"] == "utf8_string" {
                strings
                    .entry(frame["node_id"].as_u64().unwrap())
                    .or_default()
                    .push_str(frame["text"].as_str().unwrap());
            }
        }
        assert!(strings.values().any(|candidate| candidate == &value));
    }

    #[test]
    fn frame_encoder_chunks_nested_large_object_keys_and_values() {
        let large_key = "키".repeat(400_000);
        let large_value = "값🙂".repeat(210_000);
        let mut schema = serde_json::Map::new();
        schema.insert(large_key.clone(), json!([{"inner": large_value.clone()}]));
        let event = json!({
            "event": "result",
            "request_id": "nested-frame-1",
            "command": "schema.inspect",
            "success": true,
            "schema": Value::Object(schema)
        });

        let frames = collect_protocol_frames(&event).expect("encode nested frames");
        let mut strings = BTreeMap::<u64, String>::new();
        for bytes in &frames {
            assert!(bytes.len() <= MAX_JSONL_FRAME_BYTES);
            let frame = serde_json::from_slice::<Value>(bytes).expect("parse nested frame");
            if frame["value_kind"] == "utf8_string" {
                strings
                    .entry(frame["node_id"].as_u64().unwrap())
                    .or_default()
                    .push_str(frame["text"].as_str().unwrap());
            }
        }

        assert!(strings.values().any(|candidate| candidate == &large_key));
        assert!(strings.values().any(|candidate| candidate == &large_value));
    }

    #[test]
    fn frame_emit_failure_is_fused_and_stops_followup_events() {
        let request = Request {
            command: "preflight".to_string(),
            request_id: Some("emit-failure-1".to_string()),
            payload: json!({
                "source_engine": "mysql",
                "target_engine": "postgresql",
                "schema": {"tables": []}
            }),
        };
        let mut calls = 0;

        let error = handle_request_streaming(request, |_event| {
            calls += 1;
            Err(ProtocolEmitError::io("simulated broken pipe"))
        })
        .expect_err("emitter failure must propagate");

        assert_eq!(calls, 1);
        assert!(!error.side_effect_started());
    }

    #[test]
    fn frame_emit_error_can_mark_post_side_effect_indeterminate_boundary() {
        let error = ProtocolEmitError::io("simulated encode failure").after_side_effect();

        assert!(error.side_effect_started());
    }

    #[test]
    fn endpoint_query_emit_failure_is_always_indeterminate() {
        for sql in [
            "SELECT 1",
            "VALUES (1)",
            "SHOW TABLES",
            "SHOW STATUS WHERE GET_LOCK('wire-boundary', 0) = 1",
            "DESC users",
            "DESCRIBE users",
            "TABLE users",
            "UPDATE users SET name='changed'",
            "SELECT * FROM users INTO OUTFILE '/tmp/users.tsv'",
        ] {
            let request = Request {
                command: "query.execute".to_string(),
                request_id: Some("endpoint-query-boundary".to_string()),
                payload: json!({
                    "sql": sql,
                    "endpoint": {
                        "engine": "mysql",
                        "host": "127.0.0.1",
                        "port": 3306,
                        "user": "root",
                        "password": "secret",
                        "database": "app"
                    }
                }),
            };
            let (events, side_effect_started) = query_execute_with(&request, |_endpoint, _sql| {
                Ok(QueryExecutionResult {
                    rows: Vec::new(),
                    columns: Vec::new(),
                    rows_affected: 1,
                })
            });

            let error = emit_operation_events(events, side_effect_started, |_event| {
                Err(ProtocolEmitError::io("endpoint query sink failed"))
            })
            .expect_err("query emitter failure must propagate");

            assert!(
                error.side_effect_started(),
                "emission failure must be indeterminate for {sql}"
            );
        }
    }

    fn collect_protocol_frames(event: &Value) -> Result<Vec<Vec<u8>>, ProtocolEmitError> {
        let mut frames = Vec::new();
        encode_protocol_frames(event, |frame| {
            frames.push(frame.to_vec());
            Ok(())
        })?;
        Ok(frames)
    }

    #[test]
    fn normalized_error_events_always_carry_the_request_command() {
        let request = Request {
            command: "unknown.command".to_string(),
            request_id: Some("error-command-1".to_string()),
            payload: json!({}),
        };
        let mut events = Vec::new();

        handle_request_streaming(request, |event| events.push(event)).unwrap();

        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["event"], "error");
        assert_eq!(events[0]["command"], "unknown.command");
        assert!(matches!(events[0]["code"].as_str(), Some(value) if !value.is_empty()));
    }

    #[test]
    fn chunk_encoder_streams_and_stops_serializing_after_first_emit_failure() {
        let event = json!({
            "event": "result",
            "request_id": "stream-failure-1",
            "command": "query.execute",
            "success": true,
            "rows": (0..500).map(|index| json!({"id": index, "value": "x".repeat(32)})).collect::<Vec<_>>()
        });
        reset_protocol_encoding_metrics();
        let mut emitted = 0;

        let error = encode_protocol_frames(&event, |_frame| {
            emitted += 1;
            Err(ProtocolEmitError::io("simulated frame sink failure"))
        })
        .expect_err("frame sink failure must stop the encoder");
        let failed_metrics = protocol_encoding_metrics();

        assert_eq!(emitted, 1);
        assert!(!error.side_effect_started());
        assert_eq!(failed_metrics.emitted_frames, 0);
        assert!(failed_metrics.serialized_frames < 10);
        assert!(failed_metrics.visited_nodes < 10);
    }

    #[test]
    fn chunk_encoder_serialization_work_is_linear_in_payload_size() {
        fn measure(row_count: usize) -> ProtocolEncodingMetrics {
            let event = json!({
                "event": "result",
                "request_id": "linear-encoding-1",
                "command": "query.execute",
                "success": true,
                "rows": (0..row_count).map(|index| json!({"id": index, "value": "x".repeat(32)})).collect::<Vec<_>>()
            });
            reset_protocol_encoding_metrics();
            encode_protocol_frames_with_limit(&event, 4_096, &mut |_frame| Ok(())).unwrap();
            protocol_encoding_metrics()
        }

        let small = measure(250);
        let large = measure(500);

        assert!(small.emitted_frames > 1);
        assert!(large.emitted_frames > small.emitted_frames);
        assert!(large.serialized_bytes <= small.serialized_bytes * 3);
        assert!(large.serialized_frames <= small.serialized_frames * 3);
        assert!(large.peak_buffered_frames <= 1);
        assert!(large.peak_buffered_bytes <= 4_096);
    }

    #[test]
    fn chunk_encoder_bounds_reference_pages_for_high_cardinality_lists() {
        let event = json!({
            "event": "result",
            "request_id": "bounded-ref-pages",
            "command": "query.execute",
            "success": true,
            "values": (0..1_000).map(|index| format!("value-{index:04}"))
                .collect::<Vec<_>>()
        });
        reset_protocol_encoding_metrics();

        encode_protocol_frames_with_limit(&event, 4_096, &mut |_frame| Ok(())).unwrap();
        let metrics = protocol_encoding_metrics();

        assert!(metrics.emitted_frames > 1);
        assert_eq!(metrics.peak_reference_page_items, MAX_REFERENCE_PAGE_ITEMS);
    }

    #[test]
    fn chunk_encoder_rejects_aggregate_node_overflow_before_emitting() {
        let event = json!({
            "event": "result",
            "request_id": "aggregate-node-limit",
            "command": "query.execute",
            "success": true,
            "values": vec![0; MAX_ASSEMBLED_EVENT_NODES]
        });
        let mut emitted = 0;

        let error = encode_protocol_frames_with_limit(&event, 4_096, &mut |_frame| {
            emitted += 1;
            Ok(())
        })
        .expect_err("aggregate node overflow must fail preflight");

        assert_eq!(error.code(), "protocol_aggregate_nodes_exceeded");
        assert_eq!(emitted, 0);
    }

    #[test]
    fn chunk_encoder_rejects_aggregate_chunk_overflow_before_emitting() {
        let event = json!({
            "event": "result",
            "request_id": "aggregate-chunk-limit",
            "command": "query.execute",
            "success": true,
            "values": (0..MAX_ASSEMBLED_EVENT_CHUNKS)
                .map(|index| format!("value-{index:04}"))
                .collect::<Vec<_>>()
        });
        let mut emitted = 0;

        let error = encode_protocol_frames_with_limit(&event, 4_096, &mut |_frame| {
            emitted += 1;
            Ok(())
        })
        .expect_err("aggregate chunk overflow must fail preflight");

        assert_eq!(error.code(), "protocol_aggregate_chunks_exceeded");
        assert_eq!(emitted, 0);
    }

    #[test]
    fn chunk_encoder_rejects_aggregate_depth_overflow_before_emitting() {
        let mut nested = json!(0);
        for _ in 0..MAX_ASSEMBLED_EVENT_DEPTH {
            nested = json!([nested]);
        }
        let event = json!({
            "event": "result",
            "request_id": "aggregate-depth-limit",
            "command": "query.execute",
            "success": true,
            "value": nested
        });
        let mut emitted = 0;

        let error = encode_protocol_frames_with_limit(&event, 4_096, &mut |_frame| {
            emitted += 1;
            Ok(())
        })
        .expect_err("aggregate depth overflow must fail preflight");

        assert_eq!(error.code(), "protocol_aggregate_depth_exceeded");
        assert_eq!(emitted, 0);
    }

    #[test]
    fn chunk_encoder_rejects_aggregate_byte_overflow_before_emitting() {
        let event = json!({
            "event": "result",
            "request_id": "aggregate-byte-limit",
            "command": "query.execute",
            "success": true,
            "value": "x".repeat(MAX_ASSEMBLED_EVENT_BYTES)
        });
        let mut emitted = 0;

        let error = encode_protocol_frames(&event, |_frame| {
            emitted += 1;
            Ok(())
        })
        .expect_err("aggregate byte overflow must fail preflight");

        assert_eq!(error.code(), "protocol_aggregate_bytes_exceeded");
        assert_eq!(emitted, 0);
    }

    #[test]
    fn chunk_encoder_accounts_for_multi_digit_string_sequences() {
        let event = json!({
            "event": "result",
            "request_id": "string-sequence-width",
            "command": "query.execute",
            "success": true,
            "value": "x".repeat(8_000)
        });
        let mut frames = Vec::new();

        encode_protocol_frames_with_limit(&event, 512, &mut |frame| {
            frames.push(frame.to_vec());
            Ok(())
        })
        .unwrap();

        assert!(frames.len() > 10);
        assert!(frames.iter().all(|frame| frame.len() <= 512));
    }

    #[test]
    fn chunk_encoder_accounts_for_multi_digit_collection_sequences() {
        let event = json!({
            "event": "result",
            "request_id": "collection-sequence-width",
            "command": "query.execute",
            "success": true,
            "values": (0..2_000).collect::<Vec<_>>()
        });
        let mut frames = Vec::new();

        encode_protocol_frames_with_limit(&event, 512, &mut |frame| {
            frames.push(frame.to_vec());
            Ok(())
        })
        .unwrap();

        assert!(frames.len() > 10);
        assert!(frames.iter().all(|frame| frame.len() <= 512));
    }
}
