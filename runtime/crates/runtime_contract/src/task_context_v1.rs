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
                    {
                        if let Some(data) = object.get_mut("data").and_then(Value::as_object_mut) {
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
    format!(
        "Task Context Capsule {}\nEvidence only; no apply/admission or source freshness claim.\nPresentation: {}\n{}",
        digest.as_str().expect("digest string"),
        PRESENTATION_VERSION,
        body_json
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
            let body: Value = serde_json::from_str(result.lines().last().unwrap()).unwrap();
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
        serde_json::from_str(presentation(value).lines().last().unwrap()).unwrap()
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
        assert!(rendered.starts_with(&format!(
            "Task Context Capsule {}\n",
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
