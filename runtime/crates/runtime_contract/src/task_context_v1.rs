//! Python-compatible v1 capsule bytes and lossless local presentation.
//! Never use this hash domain for unrelated artifact schemas.
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Write};
use std::path::{Component, Path, PathBuf};

pub const PRESENTATION_VERSION: &str = "task_context_presentation_v1";

pub(super) fn non_null_optional_string<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    // Missing fields use default(None); explicit null must not disappear before
    // digest verification through skip_serializing_if.
    String::deserialize(deserializer).map(Some)
}

fn ascii_string(value: &str) -> String {
    let encoded = serde_json::to_string(value).expect("JSON string");
    let mut out = String::with_capacity(encoded.len());
    for ch in encoded.chars() {
        // Python also escapes DEL and encodes non-BMP scalars as UTF-16 pairs.
        if ch as u32 >= 0x7f {
            let mut buffer = [0u16; 2];
            for unit in ch.encode_utf16(&mut buffer) {
                write!(&mut out, "\\u{unit:04x}").expect("String write");
            }
        } else {
            out.push(ch);
        }
    }
    out
}

fn python_number(number: &Number) -> String {
    if number.is_i64() || number.is_u64() {
        return number.to_string();
    }
    // Retain the shortest round-trip significand; normalize only notation.
    // Python repr uses fixed notation for exponents [-4,16), a decimal suffix
    // on integral floats, and an explicit sign/two digits on small exponents.
    let text = number.to_string();
    let (sign, unsigned) = if let Some(rest) = text.strip_prefix('-') {
        ("-", rest)
    } else {
        ("", text.as_str())
    };
    let (mantissa, exponent) = if let Some((m, e)) = unsigned.split_once('e') {
        (m, e.parse::<i32>().expect("JSON exponent"))
    } else {
        (unsigned, 0)
    };
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let all_digits = mantissa.replace('.', "");
    let leading = all_digits.bytes().take_while(|b| *b == b'0').count();
    if leading == all_digits.len() {
        return format!("{sign}0.0");
    }
    let digits = all_digits[leading..].trim_end_matches('0');
    let scientific_exponent = decimal + exponent - leading as i32 - 1;
    if (-4..16).contains(&scientific_exponent) {
        let point = scientific_exponent + 1;
        if point <= 0 {
            format!("{sign}0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!(
                "{sign}{}{}.0",
                digits,
                "0".repeat(point as usize - digits.len())
            )
        } else {
            let point = point as usize;
            format!("{sign}{}.{}", &digits[..point], &digits[point..])
        }
    } else {
        let fraction = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let exponent_sign = if scientific_exponent < 0 { '-' } else { '+' };
        format!(
            "{sign}{}{fraction}e{exponent_sign}{:02}",
            &digits[..1],
            scientific_exponent.unsigned_abs()
        )
    }
}

pub fn canonical_json(value: &Value) -> Result<String, String> {
    Ok(match value {
        Value::Null => "null".into(),
        Value::Bool(v) => v.to_string(),
        Value::Number(v) => python_number(v),
        Value::String(v) => ascii_string(v),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(canonical_json)
                .collect::<Result<Vec<_>, _>>()?
                .join(",")
        ),
        Value::Object(values) => {
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort();
            let fields = keys
                .into_iter()
                .map(|key| {
                    Ok(format!(
                        "{}:{}",
                        ascii_string(key),
                        canonical_json(&values[key])?
                    ))
                })
                .collect::<Result<Vec<_>, String>>()?;
            format!("{{{}}}", fields.join(","))
        }
    })
}

pub fn semantic_sha256(value: &Value) -> Result<String, String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(canonical_json(value)?.as_bytes())
    ))
}

// Decode the signed context string only if doing so cannot discard duplicate
// keys or round numeric content; otherwise retain its original text verbatim.
struct StrictJson(Value);
impl<'de> Deserialize<'de> for StrictJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct StrictVisitor;
        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = StrictJson;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON with unique keys and exact integers")
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictJson(Value::Null))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_f64<E: de::Error>(self, _: f64) -> Result<Self::Value, E> {
                Err(E::custom("opaque context number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(StrictJson(v.into()))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(StrictJson(value)) = seq.next_element()? {
                    values.push(value);
                }
                Ok(StrictJson(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut values = Map::new();
                while let Some((key, StrictJson(value))) = map.next_entry::<String, StrictJson>()? {
                    if values.insert(key, value).is_some() {
                        return Err(de::Error::custom("duplicate context key"));
                    }
                }
                Ok(StrictJson(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(StrictVisitor)
    }
}

pub const READONLY_SOURCE_OUTLINE_MARKER: &str =
    "\nExact J-Space-authorized source excerpt (context only; not a new grant):\n";
const MAXIMUM_READONLY_SOURCE_OUTLINE_BYTES: usize = 24_576;
const MAXIMUM_READONLY_SOURCE_OUTLINE_FILES: usize = 32;
pub const READONLY_SOURCE_OUTLINE_NOTICE: &str = concat!(
    "Source outline, not runtime dependency resolution or complete implementation evidence. ",
    "Definitions: [qualified_name, f=function/c=class, start, end], including decorators. ",
    "Imports/aliases: [start, end, normalized declaration], verified source syntax: reuse ",
    "for declaration-level claims without rereading solely to confirm that syntax. Imports ",
    "match granted module paths syntactically; aliases are not evaluated. Globals: ",
    "[bound_names, start, end]. Definitions/globals are locators, not body/value evidence. ",
    "Only direct module bindings are listed, in source order; conditional/dynamic bindings ",
    "and unmatched imports are omitted. Declarations do not prove runtime resolution or ",
    "absence of rebinding. Read missing behavior with source_read and expected_sha256. ",
    "Collect conclusion-relevant helper/exception definitions with their callers before ",
    "final synthesis; batch known independent reads, not every symbol or whole files. ",
    "Existing read scopes and limits are unchanged.",
);

/// Validated capsule expectations only. J-Space still admits every actual read.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CapsuleSourceReadBinding {
    capsule_semantic_sha256: String,
    jspace_authorization_semantic_sha256: String,
    jspace_contract_semantic_sha256: String,
    repo_root: PathBuf,
    source_sha256: BTreeMap<String, String>,
}

impl CapsuleSourceReadBinding {
    pub fn capsule_semantic_sha256(&self) -> &str {
        &self.capsule_semantic_sha256
    }

    pub fn jspace_authorization_semantic_sha256(&self) -> &str {
        &self.jspace_authorization_semantic_sha256
    }

    pub fn repo_root(&self) -> &Path {
        &self.repo_root
    }

    pub fn source_sha256(&self) -> &BTreeMap<String, String> {
        &self.source_sha256
    }

    pub fn digest(&self, path: &str) -> Option<&str> {
        self.source_sha256.get(path).map(String::as_str)
    }

    pub fn validate_against(&self, contract: &Value, root: &Path) -> Result<(), String> {
        let scopes = readonly_source_scopes(contract, root)?;
        if !super::is_lower_sha256(&self.capsule_semantic_sha256)
            || self.source_sha256.values().any(|digest| !super::is_lower_sha256(digest))
            || self.repo_root.as_os_str() != root.as_os_str()
            || contract.get("authorization_semantic_sha256").and_then(Value::as_str)
                != Some(self.jspace_authorization_semantic_sha256.as_str())
            || semantic_sha256(contract)? != self.jspace_contract_semantic_sha256
            || self.source_sha256.keys().cloned().collect::<BTreeSet<_>>() != scopes
        {
            return Err("CAPSULE_SOURCE_READ_BINDING_MISMATCH: changed contract or root".into());
        }
        Ok(())
    }
}

fn source_binding_error(detail: &str) -> String {
    format!("CAPSULE_SOURCE_READ_BINDING_INVALID: {detail}")
}

fn exact_python_source_path(value: &str) -> bool {
    !value.is_empty()
        && !value.bytes().any(|byte| matches!(byte, b'*' | b'?' | b'[' | b']' | b'\\' | 0))
        && value.split('/').all(|part| !matches!(part, "" | "." | ".." | ".git"))
        && !Path::new(value).is_absolute()
        && Path::new(value).extension().is_some_and(|extension| extension == "py")
}

fn exact_root(value: &Value, root: &Path) -> bool {
    value.as_str().is_some_and(|value| Path::new(value).as_os_str() == root.as_os_str())
}

fn absent_or_empty_array(contract: &Value, key: &str) -> bool {
    contract.get(key).is_none_or(|value| value.as_array().is_some_and(Vec::is_empty))
}

fn readonly_source_scopes(contract: &Value, root: &Path) -> Result<BTreeSet<String>, String> {
    let normalized: PathBuf = root.components().collect();
    if !root.is_absolute()
        || normalized.as_os_str() != root.as_os_str()
        || root.components().any(|part| !matches!(part, Component::RootDir | Component::Normal(_)))
        || !exact_root(&contract["repo_root"], root)
        || contract["source_read"] != true
        || contract["write_scopes"].as_array().is_none_or(|values| !values.is_empty())
        || ["mapped_source_reads", "command_templates", "verifier_commands", "verifier_grants",
            "verifier_command_templates"].iter().any(|key| !absent_or_empty_array(contract, key))
    {
        return Err(source_binding_error("expected exact read-only contract and root"));
    }
    let generation = contract.get("dcf_generation").and_then(Value::as_object)
        .ok_or_else(|| source_binding_error("missing DCF generation"))?;
    if generation.get("generation_id").and_then(Value::as_str).is_none_or(|id| id.trim().is_empty())
        || generation.get("repo_root").is_some_and(|value| !exact_root(value, root))
    {
        return Err(source_binding_error("invalid DCF generation"));
    }
    let allowed = contract.get("allowed_operations").and_then(Value::as_array)
        .ok_or_else(|| source_binding_error("missing allowed operations"))?;
    let denied = contract.get("denied_operations").and_then(Value::as_array)
        .ok_or_else(|| source_binding_error("missing denied operations"))?;
    if allowed.len() != 2
        || !allowed.iter().any(|operation| operation == "read")
        || !allowed.iter().any(|operation| operation == "command")
        || denied.iter().any(|operation| operation.as_str().is_none()
            || operation == "read" || operation == "command")
    {
        return Err(source_binding_error("operations are not read/command only"));
    }
    let scopes = contract.get("read_scopes").and_then(Value::as_array)
        .ok_or_else(|| source_binding_error("missing exact read scopes"))?;
    if !(1..=MAXIMUM_READONLY_SOURCE_OUTLINE_FILES).contains(&scopes.len()) {
        return Err(source_binding_error("read scope count is outside 1..32"));
    }
    let mut exact = BTreeSet::new();
    for scope in scopes {
        let name = scope.as_str().filter(|name| exact_python_source_path(name))
            .ok_or_else(|| source_binding_error("read scope is not an exact Python path"))?;
        if !exact.insert(name.to_owned()) {
            return Err(source_binding_error("duplicate read scope"));
        }
    }
    Ok(exact)
}

pub(super) fn readonly_source_read_binding(
    capsule: &super::TaskContextCapsule,
    contract: &Value,
    root: &Path,
) -> Result<Option<CapsuleSourceReadBinding>, String> {
    capsule.validate()?;
    let Some((prose, suffix)) = capsule.context_summary.split_once(READONLY_SOURCE_OUTLINE_MARKER) else {
        return Ok(None);
    };
    if prose.trim().is_empty()
        || suffix.contains(READONLY_SOURCE_OUTLINE_MARKER)
        || suffix.len() > MAXIMUM_READONLY_SOURCE_OUTLINE_BYTES
    {
        return Err(source_binding_error("outline marker or byte bound is invalid"));
    }
    capsule.bind_jspace(Some(contract))?;
    let authorization = contract.get("authorization_semantic_sha256").and_then(Value::as_str)
        .filter(|digest| super::is_lower_sha256(digest))
        .ok_or_else(|| source_binding_error("missing J-Space authorization digest"))?;
    let scopes = readonly_source_scopes(contract, root)?;
    if !capsule.surface.is_object() || !capsule.dcf_generation.is_object() {
        return Err(source_binding_error("capsule source metadata is not an object"));
    }
    for value in [capsule.surface.get("repo_root"), capsule.dcf_generation.get("repo_root")]
        .into_iter().flatten()
    {
        if !exact_root(value, root) {
            return Err(source_binding_error("capsule root differs from J-Space root"));
        }
    }
    if capsule.dcf_generation.get("generation_id").is_some_and(|generation|
        generation.as_str().is_none_or(|id| id.trim().is_empty())
            || generation != &contract["dcf_generation"]["generation_id"])
    {
        return Err(source_binding_error("capsule DCF generation differs from J-Space"));
    }
    let StrictJson(outline) = serde_json::from_str::<StrictJson>(suffix)
        .map_err(|error| source_binding_error(&format!("malformed outline JSON: {error}")))?;
    if outline.as_object().is_none_or(|object| object.len() != 3)
        || outline["kind"] != "readonly_source_outline"
        || outline["notice"] != READONLY_SOURCE_OUTLINE_NOTICE
    {
        return Err(source_binding_error("foreign outline kind"));
    }
    let files = outline.get("files").and_then(Value::as_array)
        .ok_or_else(|| source_binding_error("missing outline files"))?;
    if !(1..=MAXIMUM_READONLY_SOURCE_OUTLINE_FILES).contains(&files.len()) {
        return Err(source_binding_error("outline file count is outside 1..32"));
    }
    let mut source_sha256 = BTreeMap::new();
    for file in files {
        let fields = file.as_object().ok_or_else(|| source_binding_error("outline file is not an object"))?;
        if !["path", "source_sha256", "definitions"].iter().all(|key| fields.contains_key(*key))
            || fields.keys().any(|key| !matches!(key.as_str(),
                "path" | "source_sha256" | "definitions" | "aliases" | "imports" | "globals"))
        {
            return Err(source_binding_error("unexpected outline file fields"));
        }
        let path = file.get("path").and_then(Value::as_str)
            .filter(|path| exact_python_source_path(path))
            .ok_or_else(|| source_binding_error("outline path is not an exact Python path"))?;
        let digest = file.get("source_sha256").and_then(Value::as_str)
            .filter(|digest| super::is_lower_sha256(digest))
            .ok_or_else(|| source_binding_error("outline hash is not lowercase SHA-256"))?;
        if source_sha256.insert(path.to_owned(), digest.to_owned()).is_some() {
            return Err(source_binding_error("duplicate outline path"));
        }
    }
    if source_sha256.keys().cloned().collect::<BTreeSet<_>>() != scopes {
        return Err(source_binding_error("outline paths differ from exact read scopes"));
    }
    Ok(Some(CapsuleSourceReadBinding {
        capsule_semantic_sha256: capsule.semantic_sha256.clone(),
        jspace_authorization_semantic_sha256: authorization.into(),
        jspace_contract_semantic_sha256: semantic_sha256(contract)?,
        repo_root: root.to_path_buf(),
        source_sha256,
    }))
}

const LOCAL_SOURCE_SECTIONS_MARKER: &str =
    "\nExact local source sections (context only; not a new grant):\n";

fn local_source_summary(summary: &str, generation: &Value) -> Option<Value> {
    // Recognize only the installed local compiler's bounded, bound projection.
    // These hashes verify capsule evidence, never current files or admission.
    if summary.chars().count() > 12_000
        || generation.get("context_mode")?.as_str()? != "local_workspace_jspace"
        || generation.get("dcf_available")?.as_bool()?
    {
        return None;
    }
    let metadata = generation.get("source_sections")?.as_array()?;
    if !(1..=4).contains(&metadata.len()) {
        return None;
    }
    let (prose, suffix) = summary.split_once(LOCAL_SOURCE_SECTIONS_MARKER)?;
    if prose.trim().is_empty() || suffix.contains(LOCAL_SOURCE_SECTIONS_MARKER) {
        return None;
    }
    let StrictJson(sections) = serde_json::from_str::<StrictJson>(suffix).ok()?;
    // Exact re-encoding proves reversibility, including Python ASCII escapes.
    if canonical_json(&sections).ok()? != suffix {
        return None;
    }
    let entries = sections.as_array()?;
    if entries.len() != metadata.len() {
        return None;
    }
    let mut total_bytes = 0;
    for (entry, binding) in entries.iter().zip(metadata) {
        let entry = entry.as_object()?;
        let binding = binding.as_object()?;
        if entry.len() != 6
            || binding.len() != 5
            || [
                "path",
                "start_line",
                "end_line",
                "source_sha256",
                "section_sha256",
            ]
            .iter()
            .any(|key| entry.get(*key) != binding.get(*key))
        {
            return None;
        }
        let start = binding.get("start_line")?.as_u64()?;
        let end = binding.get("end_line")?.as_u64()?;
        let lines = end.checked_sub(start)?.checked_add(1)?;
        let text = entry.get("text")?.as_str()?;
        if binding.get("path")?.as_str()?.is_empty()
            || start == 0
            || lines > 120
            || text.len() > 6_144
            || text.lines().count() as u64 != lines
        {
            return None;
        }
        for key in ["source_sha256", "section_sha256"] {
            let digest = binding.get(key)?.as_str()?;
            if digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return None;
            }
        }
        if format!("{:x}", Sha256::digest(text.as_bytes()))
            != binding.get("section_sha256")?.as_str()?
        {
            return None;
        }
        total_bytes += text.len();
    }
    if total_bytes > 10_000 {
        return None;
    }
    let mut context = Map::new();
    context.insert("prose".into(), prose.into());
    context.insert(
        "source_sections_marker".into(),
        LOCAL_SOURCE_SECTIONS_MARKER.into(),
    );
    context.insert("source_sections".into(), sections);
    Some(Value::Object(context))
}

pub(super) fn presentation(capsule: &Value) -> String {
    let mut body = capsule.clone();
    let digest = body
        .as_object_mut()
        .expect("capsule object")
        .remove("semantic_sha256")
        .expect("capsule digest");
    let mut inherited = Vec::new();
    let mut data_inherited = Vec::new();
    if let Some(summary) = body.get("context_summary").and_then(Value::as_str) {
        if let Ok(StrictJson(mut context)) = serde_json::from_str::<StrictJson>(summary) {
            if context.is_object() || context.is_array() {
                if let Some(object) = context.as_object_mut() {
                    for key in ["mission", "surface", "dcf_generation"] {
                        if object
                            .get(key)
                            .is_some_and(|value| Some(value) == body.get(key))
                        {
                            object.remove(key);
                            inherited.push(Value::String(key.into()));
                        }
                    }
                    // DCF's real default projection nests these fields in data.
                    // Preserve unknown schemas and every non-identical value.
                    if object.get("schema_version").and_then(Value::as_str)
                        == Some("utm-dcf-generic-task/v1")
                        && let Some(data) = object.get_mut("data").and_then(Value::as_object_mut) {
                            for key in ["mission", "surface", "dcf_generation"] {
                                if data
                                    .get(key)
                                    .is_some_and(|value| Some(value) == body.get(key))
                                {
                                    data.remove(key);
                                    data_inherited.push(Value::String(key.into()));
                                }
                            }
                        }
                }
                body["context_summary"] = context;
            }
        } else if let Some(context) = local_source_summary(summary, &body["dcf_generation"]) {
            body["context_summary"] = context;
        }
    }
    if !inherited.is_empty() {
        body["context_summary_inherits"] = inherited.into();
    }
    if !data_inherited.is_empty() {
        body["context_summary_data_inherits"] = data_inherited.into();
    }
    // ASCII canonicalization plus a Unicode decoding pass would risk escapes;
    // ordinary sorted UTF-8 JSON is appropriate for presentation, not identity.
    let body_json = super::canonical_json(&body);
    // The digest differs per member and per capsule while the body is mostly
    // shared by a batch (authority, DCF generation, source outline). Keep the
    // digest after the body so provider prompt caching can reuse that prefix.
    format!(
        "Task Context Capsule\nEvidence only; no apply/admission or source freshness claim.\nPresentation: {}\n{}\nCapsule digest: {}",
        PRESENTATION_VERSION,
        body_json,
        digest.as_str().expect("digest string"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{TASK_CONTEXT_CAPSULE_SCHEMA_VERSION, TaskContextCapsule};
    use serde_json::json;

    fn capsule() -> Value {
        let mut value = json!({"schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
            "mission": {"mission_id":"m", "mode":"RESEARCH", "objective":"保留😀", "current_predicate":"p"},
            "context_summary":"bounded", "dcf_generation":{}, "surface":{}, "authority":{},
            "evidence_refs":[{"id":"source", "kind":"source"}], "focused_verifiers":[],
            "jspace_semantic_sha256":"a".repeat(64)});
        value["semantic_sha256"] = json!(semantic_sha256(&value).unwrap());
        value
    }

    fn readonly_capsule(outline: Value) -> (TaskContextCapsule, Value) {
        let contract = json!({"repo_root":"/workspace", "source_read":true,
            "read_scopes":["src/one.py"], "write_scopes":[],
            "allowed_operations":["read","command"], "denied_operations":["modify"],
            "dcf_generation":{"generation_id":"generation-one","repo_root":"/workspace"},
            "authorization_semantic_sha256":"a".repeat(64)});
        let mut value = capsule();
        value["context_summary"] = json!(format!("Bounded source{}{}", READONLY_SOURCE_OUTLINE_MARKER, outline));
        value["dcf_generation"] = contract["dcf_generation"].clone();
        value.as_object_mut().unwrap().remove("semantic_sha256");
        value["semantic_sha256"] = json!(semantic_sha256(&value).unwrap());
        (TaskContextCapsule::from_value(value).unwrap(), contract)
    }

    fn readonly_outline() -> Value {
        json!({"kind":"readonly_source_outline", "notice":READONLY_SOURCE_OUTLINE_NOTICE,
            "files":[{"path":"src/one.py", "source_sha256":"b".repeat(64),
                "definitions":[], "globals":[[["value"],1,1]]}]})
    }

    #[test]
    fn capsule_source_binding_is_typed_bound_and_roundtrips() {
        let (capsule, contract) = readonly_capsule(readonly_outline());
        let binding = capsule.readonly_source_read_binding(&contract, Path::new("/workspace")).unwrap().unwrap();
        assert_eq!(binding.digest("src/one.py"), Some("b".repeat(64).as_str()));
        let wire = serde_json::to_value(&binding).unwrap();
        let decoded: CapsuleSourceReadBinding = serde_json::from_value(wire.clone()).unwrap();
        decoded.validate_against(&contract, Path::new("/workspace")).unwrap();
        for field in ["capsule_semantic_sha256", "jspace_authorization_semantic_sha256", "jspace_contract_semantic_sha256"] {
            let mut malformed = wire.clone();
            malformed[field] = json!("bad");
            let malformed: CapsuleSourceReadBinding = serde_json::from_value(malformed).unwrap();
            assert!(malformed.validate_against(&contract, Path::new("/workspace")).is_err(), "{field}");
        }
        assert!(decoded.validate_against(&contract, Path::new("/foreign")).is_err());
        let mut changed = contract.clone();
        changed["dcf_generation"]["generation_id"] = json!("generation-two");
        assert!(decoded.validate_against(&changed, Path::new("/workspace")).is_err());
        let mut malformed = wire;
        malformed["source_sha256"]["src/one.py"] = json!("bad");
        let malformed: CapsuleSourceReadBinding = serde_json::from_value(malformed).unwrap();
        assert!(malformed.validate_against(&contract, Path::new("/workspace")).is_err());
        let mut tampered = capsule;
        tampered.context_summary.push(' ');
        assert!(tampered.readonly_source_read_binding(&contract, Path::new("/workspace")).is_err());
    }

    #[test]
    fn capsule_source_binding_rejects_malformed_outline_metadata() {
        for case in 0..10 {
            let mut outline = readonly_outline();
            match case {
                0 => { outline.as_object_mut().unwrap().remove("notice"); }
                1 => outline["notice"] = json!("foreign"),
                2 => outline["extra"] = json!(true),
                3 => outline["files"][0]["extra"] = json!(true),
                4 => { outline["files"][0].as_object_mut().unwrap().remove("definitions"); }
                5 => outline["files"][0]["source_sha256"] = json!("b".repeat(67)),
                6 => outline["files"][0]["path"] = json!("../one.py"),
                7 => outline["files"][0]["path"] = json!("src/outside.py"),
                8 => { let row = outline["files"][0].clone(); outline["files"].as_array_mut().unwrap().push(row); }
                _ => outline["kind"] = json!("foreign"),
            }
            let (capsule, contract) = readonly_capsule(outline);
            assert!(capsule.readonly_source_read_binding(&contract, Path::new("/workspace")).is_err(), "case {case}");
        }
        let (mut capsule, contract) = readonly_capsule(readonly_outline());
        for suffix in [r#"{"kind":"readonly_source_outline","kind":"readonly_source_outline"}"#,
            READONLY_SOURCE_OUTLINE_MARKER] {
            capsule.context_summary = format!("Source{READONLY_SOURCE_OUTLINE_MARKER}{suffix}");
            let mut wire = serde_json::to_value(&capsule).unwrap();
            wire.as_object_mut().unwrap().remove("semantic_sha256");
            capsule.semantic_sha256 = semantic_sha256(&wire).unwrap();
            assert!(capsule.readonly_source_read_binding(&contract, Path::new("/workspace")).is_err());
        }
    }

    #[test]
    fn capsule_source_binding_rejects_nonreadonly_grants() {
        let (bound_capsule, contract) = readonly_capsule(readonly_outline());
        for key in ["write_scopes", "mapped_source_reads", "verifier_commands", "command_templates"] {
            let mut changed = contract.clone();
            changed[key] = json!(["src/one.py"]);
            assert!(bound_capsule.readonly_source_read_binding(&changed, Path::new("/workspace")).is_err(), "{key}");
        }
        let capsule = TaskContextCapsule::from_value(capsule()).unwrap();
        assert!(capsule.readonly_source_read_binding(&contract, Path::new("/workspace")).unwrap().is_none());
    }
    #[test]
    fn python_ascii_golden_vectors() {
        assert_eq!(
            canonical_json(&json!({"objective":"保留😀", "中文":"值"})).unwrap(),
            r#"{"objective":"\u4fdd\u7559\ud83d\ude00","\u4e2d\u6587":"\u503c"}"#
        );
        assert_eq!(
            canonical_json(&json!("\u{7f}\n\t\u{8}\u{c}\\\"")).unwrap(),
            r#""\u007f\n\t\b\f\\\"""#
        );
        assert_eq!(
            canonical_json(&json!([i64::MIN, u64::MAX, true, Value::Null])).unwrap(),
            "[-9223372036854775808,18446744073709551615,true,null]"
        );
    }
    #[test]
    fn python_finite_float_notation() {
        assert_eq!(
            canonical_json(&json!([0.1, 1.0, -0.0, 1e-4, 1e-5, 1e15, 1e16, 1.234e30])).unwrap(),
            "[0.1,1.0,-0.0,0.0001,1e-05,1000000000000000.0,1e+16,1.234e+30]"
        );
    }
    #[test]
    fn unicode_capsule_and_tamper_rejection() {
        let value = capsule();
        TaskContextCapsule::from_value(value.clone()).unwrap();
        let mut tampered = value;
        tampered["mission"]["objective"] = json!("changed");
        assert!(
            TaskContextCapsule::from_value(tampered)
                .unwrap_err()
                .contains("DIGEST_MISMATCH")
        );
    }
    #[test]
    fn explicit_null_is_not_silently_removed() {
        let mut value = capsule();
        value["mission"]["task_id"] = Value::Null;
        assert!(TaskContextCapsule::from_value(value).is_err());
        let mut value = capsule();
        value["evidence_refs"][0]["sha256"] = Value::Null;
        assert!(TaskContextCapsule::from_value(value).is_err());
    }
    #[test]
    fn presentation_deduplicates_only_equal_context_and_keeps_authority() {
        let mut value = capsule();
        value["authority"] = json!({"forbidden_effects":["broker"], "extra":"kept"});
        value["context_summary"] = json!(serde_json::to_string(&json!({"mission":value["mission"], "surface":{"different":true}, "source_excerpt":"必要資料"})).unwrap());
        let result = presentation(&value);
        for expected in ["context_summary_inherits", "different", "extra", "必要資料"] {
            assert!(result.contains(expected));
        }
        assert_eq!(result.matches("保留😀").count(), 1);
    }
    #[test]
    fn duplicate_and_imprecise_summaries_stay_opaque() {
        for summary in [
            r#"{"a":1,"a":2}"#,
            r#"{"n":0.1}"#,
            r#"{"n":18446744073709551616}"#,
        ] {
            let mut value = capsule();
            value["context_summary"] = json!(summary);
            let result = presentation(&value);
            let body: Value = serde_json::from_str(presentation_json_line(&result)).unwrap();
            assert_eq!(body["context_summary"], summary);
        }
    }

    fn local_summary_capsule(texts: &[&str]) -> Value {
        let sections = Value::Array(
            texts
                .iter()
                .enumerate()
                .map(|(index, text)| {
                    json!({
                        "path": format!("crates/source_{index}.rs"),
                        "start_line": 1, "end_line": text.lines().count(),
                        "source_sha256": "b".repeat(64),
                        "section_sha256": format!("{:x}", Sha256::digest(text.as_bytes())),
                        "text": text
                    })
                })
                .collect(),
        );
        let mut metadata = sections.clone();
        for section in metadata.as_array_mut().unwrap() {
            section.as_object_mut().unwrap().remove("text");
        }
        let mut value = capsule();
        value["dcf_generation"] = json!({"context_mode":"local_workspace_jspace",
            "dcf_available":false, "source_sections":metadata});
        value["context_summary"] = json!(format!(
            "Parent prose: \"quotes\", literal \\n and 保留😀\r\nunchanged{}{}",
            LOCAL_SOURCE_SECTIONS_MARKER,
            canonical_json(&sections).unwrap()
        ));
        value.as_object_mut().unwrap().remove("semantic_sha256");
        value["semantic_sha256"] = json!(semantic_sha256(&value).unwrap());
        value
    }

    fn presentation_body(value: &Value) -> Value {
        serde_json::from_str(presentation_json_line(&presentation(value))).unwrap()
    }

    // The canonical body is the single line after the presentation marker; the
    // capsule digest follows it.
    fn presentation_json_line(rendered: &str) -> &str {
        let marker = format!("Presentation: {PRESENTATION_VERSION}\n");
        rendered.split_once(marker.as_str()).unwrap().1.lines().next().unwrap()
    }

    fn assert_opaque_presentation(value: &Value) {
        let mut body = presentation_body(value);
        assert!(body["context_summary"].is_string());
        body["semantic_sha256"] = value["semantic_sha256"].clone();
        assert_eq!(&body, value);
    }

    #[test]
    fn local_source_presentation_is_exactly_reversible_and_evidence_only() {
        let texts = [
            "\"quotes\", literal \\n and \\u4fdd\r\n保留😀\r\n",
            "no final newline: \\ \" λ",
            "line one\nline two\n",
            LOCAL_SOURCE_SECTIONS_MARKER,
        ];
        let value = local_summary_capsule(&texts);
        let before = value.clone();
        TaskContextCapsule::from_value(value.clone()).unwrap();
        let rendered = presentation(&value);
        let mut body = presentation_body(&value);
        let context = &body["context_summary"];
        assert_eq!(context.as_object().unwrap().len(), 3);
        assert_eq!(
            context["source_sections_marker"],
            LOCAL_SOURCE_SECTIONS_MARKER
        );
        for (section, text) in context["source_sections"]
            .as_array()
            .unwrap()
            .iter()
            .zip(texts)
        {
            assert_eq!(section["text"], text);
        }
        let reconstructed = format!(
            "{}{}{}",
            context["prose"].as_str().unwrap(),
            context["source_sections_marker"].as_str().unwrap(),
            canonical_json(&context["source_sections"]).unwrap()
        );
        assert_eq!(reconstructed, value["context_summary"].as_str().unwrap());
        assert_eq!(rendered.matches("\"text\":").count(), texts.len());
        assert!(rendered.starts_with("Task Context Capsule\n"));
        assert!(rendered.ends_with(&format!(
            "\nCapsule digest: {}",
            value["semantic_sha256"].as_str().unwrap()
        )));
        body["context_summary"] = json!(reconstructed);
        body["semantic_sha256"] = value["semantic_sha256"].clone();
        assert_eq!(body, value);
        assert_eq!(value, before);
    }

    #[test]
    fn malformed_or_noncanonical_local_summaries_stay_opaque() {
        let value = local_summary_capsule(&["source 保留😀\r\nlast"]);
        let summary = value["context_summary"].as_str().unwrap();
        let (prose, suffix) = summary.split_once(LOCAL_SOURCE_SECTIONS_MARKER).unwrap();
        let StrictJson(sections) = serde_json::from_str::<StrictJson>(suffix).unwrap();
        for summary in [
            summary.replace(
                LOCAL_SOURCE_SECTIONS_MARKER,
                "\nExact local source sections (context only; not a new grant):",
            ),
            summary.replace(
                LOCAL_SOURCE_SECTIONS_MARKER,
                "\r\nExact local source sections (context only; not a new grant):\r\n",
            ),
            format!("{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{LOCAL_SOURCE_SECTIONS_MARKER}{suffix}"),
            format!("{prose}{LOCAL_SOURCE_SECTIONS_MARKER}["),
            format!("{prose}{LOCAL_SOURCE_SECTIONS_MARKER}[]"),
            format!("{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{{}}"),
            format!("{prose}{LOCAL_SOURCE_SECTIONS_MARKER} {suffix}"),
            format!("{summary}\n"),
            format!(
                "{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{}",
                serde_json::to_string(&sections).unwrap()
            ),
            format!(
                "{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{}",
                suffix.replacen("\"text\":", "\"text\":\"discarded\",\"text\":", 1)
            ),
        ] {
            let mut value = value.clone();
            value["context_summary"] = json!(summary);
            assert_opaque_presentation(&value);
        }
    }

    #[test]
    fn local_source_presentation_requires_exact_metadata_and_text_hashes() {
        let value = local_summary_capsule(&["source\r\nlast"]);
        let (prose, suffix) = value["context_summary"]
            .as_str()
            .unwrap()
            .split_once(LOCAL_SOURCE_SECTIONS_MARKER)
            .unwrap();
        let StrictJson(sections) = serde_json::from_str::<StrictJson>(suffix).unwrap();
        for (key, replacement) in [
            ("path", json!("other.rs")),
            ("start_line", json!(2)),
            ("end_line", json!(3)),
            ("source_sha256", json!("c".repeat(64))),
            ("section_sha256", json!("c".repeat(64))),
            ("text", json!("edited\r\nlast")),
            ("extra", json!(true)),
        ] {
            let mut sections = sections.clone();
            sections[0][key] = replacement;
            let mut value = value.clone();
            value["context_summary"] = json!(format!(
                "{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{}",
                canonical_json(&sections).unwrap()
            ));
            assert_opaque_presentation(&value);
        }
        for (key, replacement) in [
            ("path", json!(7)),
            ("start_line", json!(0)),
            ("end_line", json!(120)),
            ("source_sha256", json!("invalid")),
            ("section_sha256", json!("c".repeat(64))),
            ("extra", json!(true)),
        ] {
            let mut sections = sections.clone();
            sections[0][key] = replacement.clone();
            let mut value = value.clone();
            value["dcf_generation"]["source_sections"][0][key] = replacement;
            value["context_summary"] = json!(format!(
                "{prose}{LOCAL_SOURCE_SECTIONS_MARKER}{}",
                canonical_json(&sections).unwrap()
            ));
            assert_opaque_presentation(&value);
        }
    }

    #[test]
    fn non_local_unbound_and_out_of_limit_summaries_stay_opaque() {
        let value = local_summary_capsule(&["source\n"]);
        for (key, replacement) in [
            ("context_mode", json!("dcf")),
            ("dcf_available", json!(true)),
            ("source_sections", json!([])),
            ("source_sections", json!({})),
        ] {
            let mut value = value.clone();
            value["dcf_generation"][key] = replacement;
            assert_opaque_presentation(&value);
        }
        let mut missing = value.clone();
        missing["dcf_generation"]
            .as_object_mut()
            .unwrap()
            .remove("source_sections");
        assert_opaque_presentation(&missing);
        missing["dcf_generation"] = json!({});
        assert_opaque_presentation(&missing);
        for value in [
            local_summary_capsule(&[]),
            local_summary_capsule(&["x"; 5]),
            local_summary_capsule(&[&"x\n".repeat(121)]),
            local_summary_capsule(&[&"x".repeat(6_145)]),
            local_summary_capsule(&[&"x".repeat(5_001), &"y".repeat(5_000)]),
            local_summary_capsule(&[&"保".repeat(2_001)]),
        ] {
            assert_opaque_presentation(&value);
        }
    }

    #[test]
    fn pure_json_dcf_data_presentation_is_unchanged() {
        let mut value = capsule();
        value["context_summary"] = json!(
            canonical_json(&json!({
                "schema_version":"utm-dcf-generic-task/v1", "extra":"kept",
                "data":{"mission":value["mission"], "surface":value["surface"],
                    "dcf_generation":value["dcf_generation"], "text":"資料 \\ \""}
            }))
            .unwrap()
        );
        let body = presentation_body(&value);
        assert_eq!(
            body["context_summary_data_inherits"],
            json!(["mission", "surface", "dcf_generation"])
        );
        assert_eq!(
            body["context_summary"],
            json!({
                "schema_version":"utm-dcf-generic-task/v1", "extra":"kept",
                "data":{"text":"資料 \\ \""}
            })
        );
        assert_eq!(body["authority"], value["authority"]);
    }
}
