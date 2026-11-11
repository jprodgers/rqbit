//! Just enough bencode to write a torrent: dictionaries are `BTreeMap`s keyed
//! by bytes, so keys always come out in the order the format requires.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Value {
    Int(u64),
    Bytes(Vec<u8>),
    List(Vec<Value>),
    Dict(Dict),
}

pub(crate) type Dict = BTreeMap<Vec<u8>, Value>;

impl Value {
    pub(crate) fn str(s: &str) -> Value {
        Value::Bytes(s.as_bytes().to_vec())
    }

    pub(crate) fn encode(&self, out: &mut Vec<u8>) {
        match self {
            Value::Int(i) => {
                out.push(b'i');
                out.extend_from_slice(i.to_string().as_bytes());
                out.push(b'e');
            }
            Value::Bytes(b) => encode_bytes(b, out),
            Value::List(l) => {
                out.push(b'l');
                for v in l {
                    v.encode(out);
                }
                out.push(b'e');
            }
            Value::Dict(d) => {
                out.push(b'd');
                for (k, v) in d {
                    encode_bytes(k, out);
                    v.encode(out);
                }
                out.push(b'e');
            }
        }
    }
}

fn encode_bytes(b: &[u8], out: &mut Vec<u8>) {
    out.extend_from_slice(b.len().to_string().as_bytes());
    out.push(b':');
    out.extend_from_slice(b);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dictionary_keys_are_sorted_by_bytes() {
        let mut d = Dict::new();
        d.insert(b"b".to_vec(), Value::Int(1));
        d.insert(b"B".to_vec(), Value::str("x"));
        d.insert(b"".to_vec(), Value::List(vec![Value::Int(0)]));
        d.insert(b"ab".to_vec(), Value::Dict(Dict::new()));
        let mut out = Vec::new();
        Value::Dict(d).encode(&mut out);
        assert_eq!(out, b"d0:li0ee1:B1:x2:abde1:bi1ee");
    }
}
