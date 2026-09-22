//! Python-compatible v1 capsule bytes and lossless local presentation.
//! Never use this hash domain for unrelated artifact schemas.
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use std::fmt::{self, Write};

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
    let (sign, unsigned) = if let Some(rest) = text.strip_prefix('-') { ("-", rest) } else { ("", text.as_str()) };
    let (mantissa, exponent) = if let Some((m, e)) = unsigned.split_once('e') {
        (m, e.parse::<i32>().expect("JSON exponent"))
    } else { (unsigned, 0) };
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let all_digits = mantissa.replace('.', "");
    let leading = all_digits.bytes().take_while(|b| *b == b'0').count();
    if leading == all_digits.len() { return format!("{sign}0.0"); }
    let digits = all_digits[leading..].trim_end_matches('0');
    let scientific_exponent = decimal + exponent - leading as i32 - 1;
    if (-4..16).contains(&scientific_exponent) {
        let point = scientific_exponent + 1;
        if point <= 0 {
            format!("{sign}0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{sign}{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            let point = point as usize;
            format!("{sign}{}.{}", &digits[..point], &digits[point..])
        }
    } else {
        let fraction = if digits.len() > 1 { format!(".{}", &digits[1..]) } else { String::new() };
        let exponent_sign = if scientific_exponent < 0 { '-' } else { '+' };
        format!("{sign}{}{fraction}e{exponent_sign}{:02}", &digits[..1], scientific_exponent.unsigned_abs())
    }
}

pub fn canonical_json(value: &Value) -> Result<String, String> {
    Ok(match value {
        Value::Null => "null".into(),
        Value::Bool(v) => v.to_string(),
        Value::Number(v) => python_number(v),
        Value::String(v) => ascii_string(v),
        Value::Array(values) => format!("[{}]", values.iter().map(canonical_json).collect::<Result<Vec<_>, _>>()?.join(",")),
        Value::Object(values) => {
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort();
            let fields = keys.into_iter().map(|key| {
                Ok(format!("{}:{}", ascii_string(key), canonical_json(&values[key])?))
            }).collect::<Result<Vec<_>, String>>()?;
            format!("{{{}}}", fields.join(","))
        }
    })
}

pub fn semantic_sha256(value: &Value) -> Result<String, String> {
    Ok(format!("{:x}", Sha256::digest(canonical_json(value)?.as_bytes())))
}

// Decode the signed context string only if doing so cannot discard duplicate
// keys or round numeric content; otherwise retain its original text verbatim.
struct StrictJson(Value);
impl<'de> Deserialize<'de> for StrictJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct StrictVisitor;
        impl<'de> Visitor<'de> for StrictVisitor {
            type Value = StrictJson;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("JSON with unique keys and exact integers") }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> { Ok(StrictJson(Value::Null)) }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> { Ok(StrictJson(v.into())) }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> { Ok(StrictJson(v.into())) }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> { Ok(StrictJson(v.into())) }
            fn visit_f64<E: de::Error>(self, _: f64) -> Result<Self::Value, E> { Err(E::custom("opaque context number")) }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> { Ok(StrictJson(v.into())) }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Self::Value, E> { Ok(StrictJson(v.into())) }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(StrictJson(value)) = seq.next_element()? { values.push(value); }
                Ok(StrictJson(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut values = Map::new();
                while let Some((key, StrictJson(value))) = map.next_entry::<String, StrictJson>()? {
                    if values.insert(key, value).is_some() { return Err(de::Error::custom("duplicate context key")); }
                }
                Ok(StrictJson(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(StrictVisitor)
    }
}

pub(super) fn presentation(capsule: &Value) -> String {
    let mut body = capsule.clone();
    let digest = body.as_object_mut().expect("capsule object").remove("semantic_sha256").expect("capsule digest");
    let mut inherited = Vec::new();
    let mut data_inherited = Vec::new();
    if let Some(summary) = body.get("context_summary").and_then(Value::as_str) {
        if let Ok(StrictJson(mut context)) = serde_json::from_str::<StrictJson>(summary) {
            if context.is_object() || context.is_array() {
                if let Some(object) = context.as_object_mut() {
                    for key in ["mission", "surface", "dcf_generation"] {
                        if object.get(key).is_some_and(|value| Some(value) == body.get(key)) {
                            object.remove(key);
                            inherited.push(Value::String(key.into()));
                        }
                    }
                    // DCF's real default projection nests these fields in data.
                    // Preserve unknown schemas and every non-identical value.
                    if object.get("schema_version").and_then(Value::as_str) == Some("utm-dcf-generic-task/v1") {
                        if let Some(data) = object.get_mut("data").and_then(Value::as_object_mut) {
                            for key in ["mission", "surface", "dcf_generation"] {
                                if data.get(key).is_some_and(|value| Some(value) == body.get(key)) {
                                    data.remove(key);
                                    data_inherited.push(Value::String(key.into()));
                                }
                            }
                        }
                    }
                }
                body["context_summary"] = context;
            }
        }
    }
    if !inherited.is_empty() { body["context_summary_inherits"] = inherited.into(); }
    if !data_inherited.is_empty() { body["context_summary_data_inherits"] = data_inherited.into(); }
    // ASCII canonicalization plus a Unicode decoding pass would risk escapes;
    // ordinary sorted UTF-8 JSON is appropriate for presentation, not identity.
    let body_json = super::canonical_json(&body);
    format!("Task Context Capsule {}\nEvidence only; no apply/admission or source freshness claim.\nPresentation: {}\n{}",
        digest.as_str().expect("digest string"), PRESENTATION_VERSION, body_json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use crate::{TaskContextCapsule, TASK_CONTEXT_CAPSULE_SCHEMA_VERSION};

    fn capsule() -> Value {
        let mut value = json!({"schema_version": TASK_CONTEXT_CAPSULE_SCHEMA_VERSION,
            "mission": {"mission_id":"m", "mode":"RESEARCH", "objective":"保留😀", "current_predicate":"p"},
            "context_summary":"bounded", "dcf_generation":{}, "surface":{}, "authority":{},
            "evidence_refs":[{"id":"source", "kind":"source"}], "focused_verifiers":[],
            "jspace_semantic_sha256":"a".repeat(64)});
        value["semantic_sha256"] = json!(semantic_sha256(&value).unwrap()); value
    }
    #[test]
    fn python_ascii_golden_vectors() {
        assert_eq!(canonical_json(&json!({"objective":"保留😀", "中文":"值"})).unwrap(),
            r#"{"objective":"\u4fdd\u7559\ud83d\ude00","\u4e2d\u6587":"\u503c"}"#);
        assert_eq!(canonical_json(&json!("\u{7f}\n\t\u{8}\u{c}\\\"")).unwrap(), r#""\u007f\n\t\b\f\\\"""#);
        assert_eq!(canonical_json(&json!([i64::MIN, u64::MAX, true, Value::Null])).unwrap(), "[-9223372036854775808,18446744073709551615,true,null]");
    }
    #[test]
    fn python_finite_float_notation() {
        assert_eq!(canonical_json(&json!([0.1, 1.0, -0.0, 1e-4, 1e-5, 1e15, 1e16, 1.234e30])).unwrap(),
            "[0.1,1.0,-0.0,0.0001,1e-05,1000000000000000.0,1e+16,1.234e+30]");
    }
    #[test]
    fn unicode_capsule_and_tamper_rejection() {
        let value = capsule(); TaskContextCapsule::from_value(value.clone()).unwrap();
        let mut tampered = value; tampered["mission"]["objective"] = json!("changed");
        assert!(TaskContextCapsule::from_value(tampered).unwrap_err().contains("DIGEST_MISMATCH"));
    }
    #[test]
    fn explicit_null_is_not_silently_removed() {
        let mut value = capsule(); value["mission"]["task_id"] = Value::Null;
        assert!(TaskContextCapsule::from_value(value).is_err());
        let mut value = capsule(); value["evidence_refs"][0]["sha256"] = Value::Null;
        assert!(TaskContextCapsule::from_value(value).is_err());
    }
    #[test]
    fn presentation_deduplicates_only_equal_context_and_keeps_authority() {
        let mut value = capsule(); value["authority"] = json!({"forbidden_effects":["broker"], "extra":"kept"});
        value["context_summary"] = json!(serde_json::to_string(&json!({"mission":value["mission"], "surface":{"different":true}, "source_excerpt":"必要資料"})).unwrap());
        let result = presentation(&value);
        for expected in ["context_summary_inherits", "different", "extra", "必要資料"] { assert!(result.contains(expected)); }
        assert_eq!(result.matches("保留😀").count(), 1);
    }
    #[test]
    fn duplicate_and_imprecise_summaries_stay_opaque() {
        for summary in [r#"{"a":1,"a":2}"#, r#"{"n":0.1}"#, r#"{"n":18446744073709551616}"#] {
            let mut value = capsule(); value["context_summary"] = json!(summary);
            let result = presentation(&value);
            let body: Value = serde_json::from_str(result.lines().last().unwrap()).unwrap();
            assert_eq!(body["context_summary"], summary);
        }
    }
}
