//! JSON-Text in `serde_json::Value` lesen, doppelte Schlüssel ablehnen.
//!
//! `serde_json::from_str::<Value>` nähme bei doppelten Schlüsseln still den
//! letzten Wert (wie Pythons `json.loads`). Für Rahmen ist das gefährlich
//! (Prüfung und Verarbeitung könnten verschiedene Werte sehen), daher lehnt der
//! Agent solche Rahmen ab.

use std::fmt;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};

struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(StrictVisitor).map(Strict)
    }
}

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Value;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("JSON-Wert")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("Zahl nicht endlich"))
    }

    fn visit_str<E>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_string()))
    }

    fn visit_string<E>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }

    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_none<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut items = Vec::new();
        while let Some(Strict(item)) = seq.next_element()? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut out = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            let Strict(value) = map.next_value()?;
            if out.insert(key, value).is_some() {
                return Err(de::Error::custom("doppelter Schlüssel"));
            }
        }
        Ok(Value::Object(out))
    }
}

/// JSON-Text lesen; doppelte Schlüssel (auf jeder Ebene) sind ein Fehler.
pub fn from_str(text: &str) -> Result<Value, serde_json::Error> {
    let Strict(value) = serde_json::from_str(text)?;
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_duplicates() {
        assert!(from_str(r#"{"a":1,"a":2}"#).is_err());
        assert!(from_str(r#"{"x":{"a":1,"a":1}}"#).is_err());
        assert!(from_str(r#"[{"a":1},{"a":1}]"#).is_ok());
        assert_eq!(
            from_str(r#"{"a":[1,2.5,"x",null,true]}"#).unwrap()["a"][1],
            2.5
        );
        assert!(from_str("NaN").is_err());
        assert!(from_str(r#""\ud800""#).is_err());
    }
}
