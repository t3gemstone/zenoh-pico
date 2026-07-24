#!/usr/bin/env python3
"""
idlc.py - IDL header generator

Usage:
    python idlc.py --idl <file.idl> --output <dir> [--header-name <name>]
                   [--language {cpp,c,csharp,python}] [--no-packed] [--zenoh]

Supported IDL constructs:
  - module  (nested; emitted as namespaces in cpp/csharp, flattened in c/python)
  - enum    (@value(n) supported)
  - bitmask (@bit_bound(n) supported)
  - struct  (primitive + enum + bitmask + nested-struct fields; no @final)

Supported output languages:
  - cpp    : C++ header  (.hpp)  -- default
  - c      : C header    (.h)
  - csharp : C# source   (.cs)
  - python : Python module (.py)
  - rust   : Rust source (.rs)

Struct hash rules:
  - Field TYPE sizes and order are used; field NAMES are ignored
  - Adding fields or reordering them changes the hash
  - Renaming fields alone does not change the hash
  - FNV-1a 32-bit algorithm is used

Packed:
  - All structs are packed by default
  - Disable with --no-packed

Zenoh (--zenoh, c and cpp only):
  - Emits static inline serialize/deserialize helpers for every struct
  - Uses ze_serializer_t / ze_deserializer_t from zenoh-pico/api/serialization.h
"""

import os
import re
import sys
import argparse

APP_VERSION = "1.3"

# ---------------------------------------------------------------------------
# Mapping from IDL types to C++ types and byte sizes
# ---------------------------------------------------------------------------

_IDL_TO_CPP: dict = {
    "boolean":            "bool",
    "char":               "char",
    "octet":              "uint8_t",
    "byte":               "uint8_t",
    "int8":               "int8_t",
    "uint8":              "uint8_t",
    "int16":              "int16_t",
    "uint16":             "uint16_t",
    "int32":              "int32_t",
    "uint32":             "uint32_t",
    "int64":              "int64_t",
    "uint64":             "uint64_t",
    "short":              "int16_t",
    "unsigned short":     "uint16_t",
    "long":               "int32_t",
    "unsigned long":      "uint32_t",
    "long long":          "int64_t",
    "unsigned long long": "uint64_t",
    "float":              "float",
    "double":             "double",
    "float32":            "float",
    "float64":            "double",
    "string":             "std::string",
}

_IDL_SIZES: dict = {
    "boolean":            1,
    "char":               1,
    "octet":              1,
    "byte":               1,
    "int8":               1,
    "uint8":              1,
    "int16":              2,
    "uint16":             2,
    "int32":              4,
    "uint32":             4,
    "int64":              8,
    "uint64":             8,
    "short":              2,
    "unsigned short":     2,
    "long":               4,
    "unsigned long":      4,
    "long long":          8,
    "unsigned long long": 8,
    "float":              4,
    "double":             8,
    "float32":            4,
    "float64":            8,
    "string":             8,
}

# IDL primitive type -> (ze_ser_fn, ze_deser_fn, cast_type)
# Enum/bitmask/struct fields are handled separately.
_IDL_ZE_PRIM: dict = {
    "boolean":            ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8",   "uint8_t"),
    "char":               ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8",   "uint8_t"),
    "octet":              ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8",   "uint8_t"),
    "byte":               ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8",   "uint8_t"),
    "int8":               ("ze_serializer_serialize_int8",    "ze_deserializer_deserialize_int8",    "int8_t"),
    "uint8":              ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8",   "uint8_t"),
    "int16":              ("ze_serializer_serialize_int16",   "ze_deserializer_deserialize_int16",   "int16_t"),
    "uint16":             ("ze_serializer_serialize_uint16",  "ze_deserializer_deserialize_uint16",  "uint16_t"),
    "short":              ("ze_serializer_serialize_int16",   "ze_deserializer_deserialize_int16",   "int16_t"),
    "unsigned short":     ("ze_serializer_serialize_uint16",  "ze_deserializer_deserialize_uint16",  "uint16_t"),
    "int32":              ("ze_serializer_serialize_int32",   "ze_deserializer_deserialize_int32",   "int32_t"),
    "uint32":             ("ze_serializer_serialize_uint32",  "ze_deserializer_deserialize_uint32",  "uint32_t"),
    "long":               ("ze_serializer_serialize_int32",   "ze_deserializer_deserialize_int32",   "int32_t"),
    "unsigned long":      ("ze_serializer_serialize_uint32",  "ze_deserializer_deserialize_uint32",  "uint32_t"),
    "int64":              ("ze_serializer_serialize_int64",   "ze_deserializer_deserialize_int64",   "int64_t"),
    "uint64":             ("ze_serializer_serialize_uint64",  "ze_deserializer_deserialize_uint64",  "uint64_t"),
    "long long":          ("ze_serializer_serialize_int64",   "ze_deserializer_deserialize_int64",   "int64_t"),
    "unsigned long long": ("ze_serializer_serialize_uint64",  "ze_deserializer_deserialize_uint64",  "uint64_t"),
    "float":              ("ze_serializer_serialize_float",   "ze_deserializer_deserialize_float",   "float"),
    "float32":            ("ze_serializer_serialize_float",   "ze_deserializer_deserialize_float",   "float"),
    "double":             ("ze_serializer_serialize_double",  "ze_deserializer_deserialize_double",  "double"),
    "float64":            ("ze_serializer_serialize_double",  "ze_deserializer_deserialize_double",  "double"),
    "string":             None,  # special handling: ze_serializer_serialize_str / ze_deserializer_deserialize_string
}

# C underlying uint type -> (ze_ser_fn, ze_deser_fn)  [for bitmask]
_C_UINT_ZE: dict = {
    "uint8_t":  ("ze_serializer_serialize_uint8",   "ze_deserializer_deserialize_uint8"),
    "uint16_t": ("ze_serializer_serialize_uint16",  "ze_deserializer_deserialize_uint16"),
    "uint32_t": ("ze_serializer_serialize_uint32",  "ze_deserializer_deserialize_uint32"),
    "uint64_t": ("ze_serializer_serialize_uint64",  "ze_deserializer_deserialize_uint64"),
}

# ---------------------------------------------------------------------------
# AST node types
# ---------------------------------------------------------------------------

class IDLEnum:
    __slots__ = ("name", "members")

    def __init__(self, name: str, members: list):
        self.name    = name
        self.members = members  # [(str_name, int_value), ...]

class IDLBitmask:
    __slots__ = ("name", "bit_bound", "bits")

    def __init__(self, name: str, bit_bound: int, bits: list):
        self.name      = name
        self.bit_bound = bit_bound
        self.bits      = bits  # [str_bit_name, ...]

class IDLStruct:
    __slots__ = ("name", "fields")

    def __init__(self, name: str, fields: list):
        self.name   = name
        self.fields = fields  # [(type_str, field_name), ...]

class IDLModule:
    __slots__ = ("name", "declarations")

    def __init__(self, name: str, declarations: list):
        self.name         = name
        self.declarations = declarations

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list:
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\n\r":
            i += 1
            continue
        if text[i:i+2] == "//":
            j = text.find("\n", i)
            i = j + 1 if j != -1 else n
            continue
        if text[i:i+2] == "/*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
            continue
        if c == "@":
            m = re.match(r"@[A-Za-z_]\w*", text[i:])
            if m:
                tokens.append(m.group())
                i += m.end()
                continue
            i += 1
            continue
        if c.isdigit():
            m = re.match(r"\d+", text[i:])
            tokens.append(m.group())
            i += m.end()
            continue
        if c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_]\w*", text[i:])
            tokens.append(m.group())
            i += m.end()
            continue
        if c in "{}();,":
            tokens.append(c)
            i += 1
            continue
        # Unknown character — skip
        i += 1
    return tokens

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: list):
        self._t = tokens
        self._i = 0

    def _peek(self, offset: int = 0):
        idx = self._i + offset
        return self._t[idx] if idx < len(self._t) else None

    def _consume(self, expected=None) -> str:
        if self._i >= len(self._t):
            raise SyntaxError(f"Unexpected end of tokens; expected: {expected!r}")
        tok = self._t[self._i]
        if expected is not None and tok != expected:
            raise SyntaxError(
                f"Expected {expected!r}, got {tok!r} (position {self._i})"
            )
        self._i += 1
        return tok

    def parse(self) -> list:
        decls = []
        while self._i < len(self._t):
            d = self._parse_decl()
            if d is not None:
                decls.append(d)
        return decls

    # ------------------------------------------------------------------
    def _parse_decl(self):
        t = self._peek()
        if t is None:
            return None
        if t == "module":
            return self._parse_module()
        if t == "enum":
            return self._parse_enum()
        if t == "struct":
            return self._parse_struct()
        if t == "@bit_bound":
            return self._parse_bitmask_annotated()
        if t == "bitmask":
            return self._parse_bitmask(32)
        if t == ";":
            self._consume(";")
            return None
        # Unknown — skip
        self._i += 1
        return None

    def _parse_module(self) -> IDLModule:
        self._consume("module")
        name = self._consume()
        self._consume("{")
        decls = []
        while self._peek() != "}":
            if self._peek() is None:
                break
            d = self._parse_decl()
            if d is not None:
                decls.append(d)
        self._consume("}")
        if self._peek() == ";":
            self._consume(";")
        return IDLModule(name, decls)

    def _parse_enum(self) -> IDLEnum:
        self._consume("enum")
        name = self._consume()
        self._consume("{")
        members = []
        auto_val = 0
        while self._peek() != "}":
            if self._peek() is None:
                break
            if self._peek() == "@value":
                self._consume("@value")
                self._consume("(")
                auto_val = int(self._consume())
                self._consume(")")
            member_name = self._consume()
            members.append((member_name, auto_val))
            auto_val += 1
            if self._peek() == ",":
                self._consume(",")
        self._consume("}")
        if self._peek() == ";":
            self._consume(";")
        return IDLEnum(name, members)

    def _parse_bitmask_annotated(self) -> IDLBitmask:
        self._consume("@bit_bound")
        self._consume("(")
        bit_bound = int(self._consume())
        self._consume(")")
        return self._parse_bitmask(bit_bound)

    def _parse_bitmask(self, bit_bound: int) -> IDLBitmask:
        self._consume("bitmask")
        name = self._consume()
        self._consume("{")
        bits = []
        while self._peek() != "}":
            if self._peek() is None:
                break
            bit_name = self._consume()
            bits.append(bit_name)
            if self._peek() == ",":
                self._consume(",")
        self._consume("}")
        if self._peek() == ";":
            self._consume(";")
        return IDLBitmask(name, bit_bound, bits)

    def _parse_struct(self) -> IDLStruct:
        self._consume("struct")
        name = self._consume()
        self._consume("{")
        fields = []
        while self._peek() != "}":
            if self._peek() is None:
                break
            result = self._parse_field()
            if result is not None:
                fields.append(result)
        self._consume("}")
        if self._peek() == ";":
            self._consume(";")
        return IDLStruct(name, fields)

    def _parse_field(self):
        t = self._peek()
        if t is None or t == "}":
            return None
        type_str   = self._parse_type_name()
        field_name = self._peek()
        if field_name is None or field_name in ("}", ";"):
            return None
        self._consume()
        if self._peek() == ";":
            self._consume(";")
        return (type_str, field_name)

    def _parse_type_name(self) -> str:
        t = self._peek()
        if t == "unsigned":
            self._consume()
            nxt = self._peek()
            if nxt == "long":
                self._consume()
                if self._peek() == "long":
                    self._consume()
                    return "unsigned long long"
                return "unsigned long"
            if nxt == "short":
                self._consume()
                return "unsigned short"
            return "unsigned"
        if t == "long":
            self._consume()
            if self._peek() == "long":
                self._consume()
                return "long long"
            return "long"
        return self._consume()

# ---------------------------------------------------------------------------
# Helpers: type registry and topological sort
# ---------------------------------------------------------------------------

def _build_registry(decls: list) -> dict:
    reg = {}
    def _walk(d):
        if isinstance(d, IDLModule):
            for sub in d.declarations:
                _walk(sub)
        elif isinstance(d, (IDLEnum, IDLBitmask, IDLStruct)):
            reg[d.name] = d
    for d in decls:
        _walk(d)
    return reg


def _topo_sort_structs(structs: list, registry: dict) -> list:
    """Sort struct list in dependency order."""
    name_set = {s.name for s in structs}
    order, visited = [], set()

    def visit(s):
        if s.name in visited:
            return
        visited.add(s.name)
        for ft, _ in s.fields:
            dep = registry.get(ft)
            if isinstance(dep, IDLStruct) and dep.name in name_set:
                visit(dep)
        order.append(s)

    for s in structs:
        visit(s)
    return order

# ---------------------------------------------------------------------------
# Hash computation: FNV-1a 32-bit
# ---------------------------------------------------------------------------

def _fnv1a_32(text: str) -> int:
    h = 0x811c9dc5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _flat_sizes(type_name: str, registry: dict, visited: frozenset = frozenset()) -> list:
    """
    Return the flattened list of byte sizes for a type.
    - Primitive types: [size]
    - Enum: [4]  (IDL standard: 32-bit)
    - Bitmask: [bit_bound/8]
    - Struct: recursively flattened sizes of its fields
    """
    if type_name in _IDL_SIZES:
        return [_IDL_SIZES[type_name]]
    node = registry.get(type_name)
    if node is None:
        return [4]  # unknown type, assume 4 bytes
    if isinstance(node, IDLEnum):
        return [4]
    if isinstance(node, IDLBitmask):
        return [(node.bit_bound + 7) // 8]
    if isinstance(node, IDLStruct):
        if type_name in visited:
            return [0]  # guard against cycles
        new_vis = visited | {type_name}
        sizes = []
        for ft, _ in node.fields:
            sizes.extend(_flat_sizes(ft, registry, new_vis))
        return sizes
    return [4]


def _compute_struct_hash(struct: IDLStruct, registry: dict) -> str:
    """
    Hash canon: "{direct_field_count}:{total_bytes}:{size1,size2,...}"
    Field NAMES are not included; only type sizes and their order are used.
    """
    flat: list = []
    for ft, _ in struct.fields:
        flat.extend(_flat_sizes(ft, registry))
    direct_count = len(struct.fields)
    total_size   = sum(flat)
    canonical    = f"{direct_count}:{total_size}:{','.join(str(s) for s in flat)}"
    h = _fnv1a_32(canonical)
    return f"{h:08x}"

# ---------------------------------------------------------------------------
# C++ code generation
# ---------------------------------------------------------------------------

# Trailing type suffixes (longest first — first match is stripped)
_TYPE_SUFFIXES = ("_et", "_st", "_t", "_s")


def _to_camel_hash_name(name: str) -> str:
    """Derive a PascalCase name from a struct name for use in kHash constants.

    - Trailing type suffix (_et, _st, _t, _s) is stripped.
    - Underscores are removed; the following letter is capitalised (camelCase).
    - The first letter is uppercased to give PascalCase.

    Examples:
      ornek_paket_t         -> OrnekPaket
      device_start_params_t -> DeviceStartParams
      cihaz_sayi_et         -> CihazSayi
      Nabiz_st              -> Nabiz
      GpsLocation_t         -> GpsLocation
    """
    for suffix in _TYPE_SUFFIXES:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    parts = name.split("_")
    camel = parts[0]
    for part in parts[1:]:
        if part:
            camel += part[0].upper() + part[1:]
    return (camel[0].upper() + camel[1:]) if camel else camel


def _bitmask_underlying(bit_bound: int) -> str:
    if bit_bound <= 8:
        return "uint8_t"
    if bit_bound <= 16:
        return "uint16_t"
    if bit_bound <= 32:
        return "uint32_t"
    return "uint64_t"


def _cpp_type(type_name: str) -> str:
    return _IDL_TO_CPP.get(type_name, type_name)


def _hex_arr_str(h: str) -> str:
    """Produce a {{0xaa, 0xbb, 0xcc, 0xdd}} array literal from a hash hex string."""
    hval = int(h, 16)
    b0, b1 = (hval >> 24) & 0xFF, (hval >> 16) & 0xFF
    b2, b3 = (hval >>  8) & 0xFF,  hval        & 0xFF
    return f"{{0x{b0:02x}, 0x{b1:02x}, 0x{b2:02x}, 0x{b3:02x}}}"


def _emit_declarations(lines: list, decls: list, registry: dict,
                       indent: str = "", packed: bool = True) -> None:
    """Convert an AST node list to C++ lines (recursive).

    Struct hashes for the current namespace level are appended in alphabetical
    order before the namespace closing brace.
    """
    # __attribute__((packed)) must precede the struct name (GCC accepts this)
    _attr = "__attribute__((packed)) " if packed else ""
    # Struct hashes at this level; emitted before the namespace closing brace
    local_hashes: list = []

    for d in decls:
        if isinstance(d, IDLModule):
            lines.append(f"{indent}namespace {d.name} {{")
            # For nested namespaces suppress the blank line between them;
            # only add a blank line when content (enum/struct/bitmask) starts
            first_decl = next((x for x in d.declarations if x is not None), None)
            if not isinstance(first_decl, IDLModule):
                lines.append("")
            _emit_declarations(lines, d.declarations, registry, indent, packed)
            lines.append(f"{indent}}} // namespace {d.name}")

        elif isinstance(d, IDLEnum):
            lines.append(f"{indent}enum class {d.name} : uint32_t {{")
            for i, (mname, mval) in enumerate(d.members):
                comma = "," if i < len(d.members) - 1 else ""
                lines.append(f"{indent}    {mname} = {mval}{comma}")
            lines.append(f"{indent}}};")
            lines.append("")

        elif isinstance(d, IDLBitmask):
            underlying = _bitmask_underlying(d.bit_bound)
            cast_type  = f"static_cast<{underlying}>"
            lines.append(f"{indent}using {d.name} = {underlying};")
            for idx, bit_name in enumerate(d.bits):
                lines.append(
                    f"{indent}static constexpr {d.name} "
                    f"{d.name}_{bit_name} = {cast_type}(1u << {idx});"
                )
            lines.append("")

        elif isinstance(d, IDLStruct):
            if not d.fields:
                lines.append(f"{indent}struct {_attr}{d.name} {{}};")
            else:
                lines.append(f"{indent}struct {_attr}{d.name} {{")
                for ft, fn in d.fields:
                    cpp_t = _cpp_type(ft)
                    lines.append(f"{indent}    {cpp_t} {fn}{{}};")
                lines.append(f"{indent}}};")
                h = _compute_struct_hash(d, registry)
                local_hashes.append((d.name, h))
            lines.append("")

    # Struct hashes at this level: all kHash*Str first, then all kHash*Hex; alphabetical order
    if local_hashes:
        local_hashes.sort(key=lambda x: x[0].casefold())
        # kHash{Name}Str — first letter capitalised, rest of struct name preserved
        cpp_named = [("kHash" + _to_camel_hash_name(n), h) for n, h in local_hashes]
        for cpp_name, h in cpp_named:
            lines.append(f"{indent}static constexpr std::string_view {cpp_name}Str = \"{h}\";")
        for cpp_name, h in cpp_named:
            lines.append(f"{indent}static constexpr uint8_t {cpp_name}Hex[4] = {_hex_arr_str(h)};")
        lines.append("")


def generate_header(decls: list, source_name: str, header_stem: str,
                    packed: bool = True, zenoh: bool = False) -> str:
    """Generate C++ header text from an IDL AST list."""
    guard   = re.sub(r"[^A-Za-z0-9]", "_", header_stem).upper()
    reg     = _build_registry(decls)
    lines   = []

    lines.append("// Auto-generated by idlc.py")
    lines.append(f"// Source: {source_name}")
    lines.append(f"#ifndef {guard}_HPP")
    lines.append(f"#define {guard}_HPP")
    lines.append("")
    lines.append("#include <cstdint>")
    lines.append("#include <string>")
    if zenoh:
        lines.append("#include <zenoh/api.hxx>")
    lines.append("")

    _emit_declarations(lines, decls, reg, packed=packed)

    if zenoh:
        lines.append("/* --- zenoh-pico serialize/deserialize helpers --- */")
        lines.append("")
        _emit_cpp_zenoh_helpers(lines, decls, reg)

    if lines and lines[-1] != "":
        lines.append("")
    lines.append(f"#endif // {guard}_HPP")
    lines.append("")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# C code generation
# ---------------------------------------------------------------------------

_IDL_TO_C: dict = {
    "boolean":            "bool",
    "char":               "char",
    "octet":              "uint8_t",
    "byte":               "uint8_t",
    "int8":               "int8_t",
    "uint8":              "uint8_t",
    "int16":              "int16_t",
    "uint16":             "uint16_t",
    "int32":              "int32_t",
    "uint32":             "uint32_t",
    "int64":              "int64_t",
    "uint64":             "uint64_t",
    "short":              "int16_t",
    "unsigned short":     "uint16_t",
    "long":               "int32_t",
    "unsigned long":      "uint32_t",
    "long long":          "int64_t",
    "unsigned long long": "uint64_t",
    "float":              "float",
    "double":             "double",
    "float32":            "float",
    "float64":            "double",
    "string":             "const char*",
}


def _c_type(type_name: str) -> str:
    return _IDL_TO_C.get(type_name, type_name)


def _bitmask_underlying_c(bit_bound: int) -> str:
    if bit_bound <= 8:
        return "uint8_t"
    if bit_bound <= 16:
        return "uint16_t"
    if bit_bound <= 32:
        return "uint32_t"
    return "uint64_t"


def _collect_flat_decls(decls: list) -> list:
    """Flatten modules and return all nested declarations as a plain list."""
    result = []
    for d in decls:
        if isinstance(d, IDLModule):
            result.extend(_collect_flat_decls(d.declarations))
        else:
            result.append(d)
    return result


def _emit_c_declarations(lines: list, decls: list, registry: dict,
                          packed: bool = True) -> None:
    """Convert IDL declarations to C lines (modules are flattened)."""
    flat = _collect_flat_decls(decls)
    structs        = [d for d in flat if isinstance(d, IDLStruct)]
    sorted_structs = _topo_sort_structs(structs, registry)
    ordered        = [d for d in flat if not isinstance(d, IDLStruct)]
    ordered.extend(sorted_structs)

    local_hashes: list = []
    attr = " __attribute__((packed))" if packed else ""

    for d in ordered:
        if isinstance(d, IDLEnum):
            lines.append(f"typedef enum {{")
            ml = max((len(m[0]) for m in d.members), default=0)
            for i, (mname, mval) in enumerate(d.members):
                comma   = "," if i < len(d.members) - 1 else ""
                padding = " " * (ml - len(mname))
                lines.append(f"    {mname}{padding} = {mval}{comma}")
            lines.append(f"}} {d.name};")
            lines.append("")

        elif isinstance(d, IDLBitmask):
            underlying = _bitmask_underlying_c(d.bit_bound)
            lines.append(f"typedef {underlying} {d.name};")
            for idx, bit_name in enumerate(d.bits):
                lines.append(
                    f"#define {d.name}_{bit_name}"
                    f"  (({d.name})(1u << {idx}))"
                )
            lines.append("")

        elif isinstance(d, IDLStruct):
            if not d.fields:
                lines.append(f"typedef struct{attr} {{}} {d.name};")
            else:
                lines.append(f"typedef struct{attr} {{")
                mt = max((len(_c_type(ft)) for ft, _ in d.fields), default=0)
                for ft, fn in d.fields:
                    ct      = _c_type(ft)
                    padding = " " * (mt - len(ct))
                    lines.append(f"    {ct}{padding} {fn};")
                lines.append(f"}} {d.name};")
                h = _compute_struct_hash(d, registry)
                local_hashes.append((d.name, h))
            lines.append("")

    if local_hashes:
        local_hashes.sort(key=lambda x: x[0].casefold())
        cpp_named = [("__HASH_" + n.upper(), h) for n, h in local_hashes]
        for cpp_name, h in cpp_named:
            lines.append(f"#define {cpp_name}_STR__ \"{h}\"")
        for cpp_name, h in cpp_named:
            lines.append(f"#define {cpp_name}_HEX__ {_hex_arr_str(h)}")
        lines.append("")


def generate_header_c(decls: list, source_name: str, header_stem: str,
                       packed: bool = True, zenoh: bool = False) -> str:
    """Generate C header text from an IDL AST list."""
    guard = re.sub(r"[^A-Za-z0-9]", "_", header_stem).upper()
    reg   = _build_registry(decls)
    lines = []

    lines.append("// Auto-generated by idlc.py")
    lines.append(f"// Source: {source_name}")
    lines.append(f"#ifndef {guard}_H")
    lines.append(f"#define {guard}_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("#include <stdbool.h>")
    if zenoh:
        lines.append("#include <zenoh-pico.h>")
        lines.append("#include <zenoh-pico/api/serialization.h>")
    lines.append("")

    _emit_c_declarations(lines, decls, reg, packed=packed)

    if zenoh:
        lines.append("/* --- zenoh-pico serialize/deserialize helpers --- */")
        lines.append("")
        _emit_c_zenoh_helpers(lines, decls, reg)

    if lines and lines[-1] != "":
        lines.append("")
    lines.append(f"#endif // {guard}_H")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# C# code generation
# ---------------------------------------------------------------------------

_IDL_TO_CSHARP: dict = {
    "boolean":            "bool",
    "char":               "byte",
    "octet":              "byte",
    "byte":               "byte",
    "int8":               "sbyte",
    "uint8":              "byte",
    "int16":              "short",
    "uint16":             "ushort",
    "int32":              "int",
    "uint32":             "uint",
    "int64":              "long",
    "uint64":             "ulong",
    "short":              "short",
    "unsigned short":     "ushort",
    "long":               "int",
    "unsigned long":      "uint",
    "long long":          "long",
    "unsigned long long": "ulong",
    "float":              "float",
    "double":             "double",
    "float32":            "float",
    "float64":            "double",
    "string":             "string",
}


def _csharp_type(type_name: str) -> str:
    return _IDL_TO_CSHARP.get(type_name, type_name)


def _bitmask_underlying_csharp(bit_bound: int) -> str:
    if bit_bound <= 8:
        return "byte"
    if bit_bound <= 16:
        return "ushort"
    if bit_bound <= 32:
        return "uint"
    return "ulong"


def _emit_csharp_declarations(lines: list, decls: list, registry: dict,
                               indent: str = "", packed: bool = True) -> None:
    """Convert AST declarations to C# lines (recursive)."""
    local_hashes: list = []
    pack_val = "1" if packed else "0"

    for d in decls:
        if isinstance(d, IDLModule):
            lines.append(f"{indent}namespace {d.name}")
            lines.append(f"{indent}{{")
            lines.append("")
            _emit_csharp_declarations(lines, d.declarations, registry,
                                      indent + "    ", packed)
            lines.append(f"{indent}}} // namespace {d.name}")
            lines.append("")

        elif isinstance(d, IDLEnum):
            lines.append(f"{indent}public enum {d.name} : uint")
            lines.append(f"{indent}{{")
            for i, (mname, mval) in enumerate(d.members):
                comma = "," if i < len(d.members) - 1 else ""
                lines.append(f"{indent}    {mname} = {mval}{comma}")
            lines.append(f"{indent}}}")
            lines.append("")

        elif isinstance(d, IDLBitmask):
            underlying = _bitmask_underlying_csharp(d.bit_bound)
            lines.append(f"{indent}[Flags]")
            lines.append(f"{indent}public enum {d.name} : {underlying}")
            lines.append(f"{indent}{{")
            for idx, bit_name in enumerate(d.bits):
                comma = "," if idx < len(d.bits) - 1 else ""
                lines.append(f"{indent}    {bit_name} = 1 << {idx}{comma}")
            lines.append(f"{indent}}}")
            lines.append("")

        elif isinstance(d, IDLStruct):
            lines.append(
                f"{indent}[StructLayout(LayoutKind.Sequential, Pack = {pack_val})]"
            )
            lines.append(f"{indent}public struct {d.name}")
            lines.append(f"{indent}{{")
            for ft, fn in d.fields:
                cs_t = _csharp_type(ft)
                lines.append(f"{indent}    public {cs_t} {fn};")
            lines.append(f"{indent}}}")
            h = _compute_struct_hash(d, registry)
            local_hashes.append((d.name, h))
            lines.append("")

    if local_hashes:
        local_hashes.sort(key=lambda x: x[0].casefold())
        cpp_named = [("kHash" + _to_camel_hash_name(n), h) for n, h in local_hashes]
        lines.append(f"{indent}public static class Hashes")
        lines.append(f"{indent}{{")
        for cpp_name, h in cpp_named:
            lines.append(
                f"{indent}    public const string {cpp_name}Str = \"{h}\";"
            )
        for cpp_name, h in cpp_named:
            hval = int(h, 16)
            b0 = (hval >> 24) & 0xFF
            b1 = (hval >> 16) & 0xFF
            b2 = (hval >>  8) & 0xFF
            b3 =  hval        & 0xFF
            lines.append(
                f"{indent}    public static readonly byte[] {cpp_name}Hex = "
                f"new byte[] {{ 0x{b0:02x}, 0x{b1:02x}, 0x{b2:02x}, 0x{b3:02x} }};"
            )
        lines.append(f"{indent}}}")
        lines.append("")


def generate_header_csharp(decls: list, source_name: str,
                            packed: bool = True) -> str:
    """Generate C# source text from an IDL AST list."""
    reg   = _build_registry(decls)
    lines = []

    lines.append("// Auto-generated by idlc.py")
    lines.append(f"// Source: {source_name}")
    lines.append("using System;")
    lines.append("using System.Runtime.InteropServices;")
    lines.append("")

    _emit_csharp_declarations(lines, decls, reg, packed=packed)

    if lines and lines[-1] != "":
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Python code generation
# ---------------------------------------------------------------------------

_IDL_TO_PYTHON: dict = {
    "boolean":            "ctypes.c_bool",
    "char":               "ctypes.c_char",
    "octet":              "ctypes.c_uint8",
    "byte":               "ctypes.c_uint8",
    "int8":               "ctypes.c_int8",
    "uint8":              "ctypes.c_uint8",
    "int16":              "ctypes.c_int16",
    "uint16":             "ctypes.c_uint16",
    "int32":              "ctypes.c_int32",
    "uint32":             "ctypes.c_uint32",
    "int64":              "ctypes.c_int64",
    "uint64":             "ctypes.c_uint64",
    "short":              "ctypes.c_int16",
    "unsigned short":     "ctypes.c_uint16",
    "long":               "ctypes.c_int32",
    "unsigned long":      "ctypes.c_uint32",
    "long long":          "ctypes.c_int64",
    "unsigned long long": "ctypes.c_uint64",
    "float":              "ctypes.c_float",
    "double":             "ctypes.c_double",
    "float32":            "ctypes.c_float",
    "float64":            "ctypes.c_double",
    "string":             "ctypes.c_char_p",
}


def _python_ctypes_type(type_name: str, registry: dict) -> str:
    if type_name in _IDL_TO_PYTHON:
        return _IDL_TO_PYTHON[type_name]
    node = registry.get(type_name)
    if isinstance(node, IDLEnum):
        return "ctypes.c_uint32"
    if isinstance(node, IDLBitmask):
        bb = node.bit_bound
        if bb <= 8:
            return "ctypes.c_uint8"
        if bb <= 16:
            return "ctypes.c_uint16"
        if bb <= 32:
            return "ctypes.c_uint32"
        return "ctypes.c_uint64"
    if isinstance(node, IDLStruct):
        return type_name
    return type_name


def _emit_python_declarations(lines: list, decls: list, registry: dict,
                               packed: bool = True) -> None:
    """Convert IDL declarations to Python lines (modules are flattened)."""
    flat = _collect_flat_decls(decls)
    structs        = [d for d in flat if isinstance(d, IDLStruct)]
    sorted_structs = _topo_sort_structs(structs, registry)
    ordered        = [d for d in flat if not isinstance(d, IDLStruct)]
    ordered.extend(sorted_structs)

    local_hashes: list = []

    for d in ordered:
        if isinstance(d, IDLEnum):
            lines.append(f"class {d.name}(IntEnum):")
            for mname, mval in d.members:
                lines.append(f"    {mname} = {mval}")
            lines.append("")

        elif isinstance(d, IDLBitmask):
            lines.append(f"class {d.name}(IntFlag):")
            for idx, bit_name in enumerate(d.bits):
                lines.append(f"    {bit_name} = 1 << {idx}")
            lines.append("")

        elif isinstance(d, IDLStruct):
            lines.append(
                f"class {d.name}(ctypes.LittleEndianStructure):"
            )
            if packed:
                lines.append(f"    _pack_ = 1")
            lines.append(f"    _fields_ = [")
            for ft, fn in d.fields:
                py_t = _python_ctypes_type(ft, registry)
                lines.append(f"        (\"{fn}\", {py_t}),")
            lines.append(f"    ]")
            h = _compute_struct_hash(d, registry)
            local_hashes.append((d.name, h))
            lines.append("")

    if local_hashes:
        local_hashes.sort(key=lambda x: x[0].casefold())
        cpp_named = [("HASH_" + _to_camel_hash_name(n), h) for n, h in local_hashes]
        for cpp_name, h in cpp_named:
            lines.append(f"{cpp_name}_STR = \"{h}\"")
        for cpp_name, h in cpp_named:
            hval = int(h, 16)
            b0 = (hval >> 24) & 0xFF
            b1 = (hval >> 16) & 0xFF
            b2 = (hval >>  8) & 0xFF
            b3 =  hval        & 0xFF
            lines.append(
                f"{cpp_name}_HEX = bytes([0x{b0:02x}, 0x{b1:02x},"
                f" 0x{b2:02x}, 0x{b3:02x}])"
            )
        lines.append("")


def generate_header_python(decls: list, source_name: str,
                            packed: bool = True) -> str:
    """Generate Python module text from an IDL AST list."""
    reg   = _build_registry(decls)
    lines = []

    lines.append("# Auto-generated by idlc.py")
    lines.append(f"# Source: {source_name}")
    lines.append("import ctypes")
    lines.append("from enum import IntEnum, IntFlag")
    lines.append("")

    _emit_python_declarations(lines, decls, reg, packed=packed)

    if lines and lines[-1] != "":
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rust code generation
# ---------------------------------------------------------------------------

_IDL_TO_RUST: dict = {
    "boolean":            "bool",
    "char":               "u8",
    "octet":              "u8",
    "byte":               "u8",
    "int8":               "i8",
    "uint8":              "u8",
    "int16":              "i16",
    "uint16":             "u16",
    "int32":              "i32",
    "uint32":             "u32",
    "int64":              "i64",
    "uint64":             "u64",
    "short":              "i16",
    "unsigned short":     "u16",
    "long":               "i32",
    "unsigned long":      "u32",
    "long long":          "i64",
    "unsigned long long": "u64",
    "float":              "f32",
    "double":             "f64",
    "float32":            "f32",
    "float64":            "f64",
    "string":             "*const i8",
}


def _bitmask_underlying_rust(bit_bound: int) -> str:
    if bit_bound <= 8:
        return "u8"
    if bit_bound <= 16:
        return "u16"
    if bit_bound <= 32:
        return "u32"
    return "u64"


def _camel_to_screaming_snake(name: str) -> str:
    """Convert a PascalCase name to SCREAMING_SNAKE_CASE.

    Examples: GpsLocation -> GPS_LOCATION, SensorData -> SENSOR_DATA
    """
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()


def _rust_type(type_name: str, registry: dict) -> str:
    if type_name in _IDL_TO_RUST:
        return _IDL_TO_RUST[type_name]
    node = registry.get(type_name)
    if isinstance(node, (IDLEnum, IDLBitmask, IDLStruct)):
        return type_name
    return type_name


def _emit_rust_declarations(lines: list, decls: list, registry: dict,
                              indent: str = "", packed: bool = True) -> None:
    """Convert AST declarations to Rust lines (recursive)."""
    local_hashes: list = []
    repr_attr = "#[repr(C, packed)]" if packed else "#[repr(C)]"

    for d in decls:
        if isinstance(d, IDLModule):
            lines.append(f"{indent}pub mod {d.name} {{")
            _emit_rust_declarations(lines, d.declarations, registry,
                                     indent + "    ", packed)
            lines.append(f"{indent}}} // mod {d.name}")
            lines.append("")

        elif isinstance(d, IDLEnum):
            lines.append(f"{indent}#[repr(u32)]")
            lines.append(f"{indent}#[derive(Debug, Clone, Copy, PartialEq, Eq)]")
            lines.append(f"{indent}pub enum {d.name} {{")
            for mname, mval in d.members:
                lines.append(f"{indent}    {mname} = {mval},")
            lines.append(f"{indent}}}")
            lines.append("")

        elif isinstance(d, IDLBitmask):
            underlying = _bitmask_underlying_rust(d.bit_bound)
            lines.append(f"{indent}pub type {d.name} = {underlying};")
            type_upper = d.name.upper()
            for idx, bit_name in enumerate(d.bits):
                lines.append(
                    f"{indent}pub const {type_upper}_{bit_name.upper()}"
                    f": {d.name} = 1 << {idx};"
                )
            lines.append("")

        elif isinstance(d, IDLStruct):
            lines.append(f"{indent}{repr_attr}")
            lines.append(f"{indent}#[derive(Debug, Clone, Copy)]")
            lines.append(f"{indent}pub struct {d.name} {{")
            for ft, fn in d.fields:
                rust_t = _rust_type(ft, registry)
                lines.append(f"{indent}    pub {fn}: {rust_t},")
            lines.append(f"{indent}}}")
            h = _compute_struct_hash(d, registry)
            local_hashes.append((d.name, h))
            lines.append("")

    if local_hashes:
        local_hashes.sort(key=lambda x: x[0].casefold())
        for name, h in local_hashes:
            rust_name = _camel_to_screaming_snake(_to_camel_hash_name(name))
            lines.append(
                f"{indent}pub const HASH_{rust_name}_STR: &str = \"{h}\";"
            )
        for name, h in local_hashes:
            rust_name = _camel_to_screaming_snake(_to_camel_hash_name(name))
            hval = int(h, 16)
            b0 = (hval >> 24) & 0xFF
            b1 = (hval >> 16) & 0xFF
            b2 = (hval >>  8) & 0xFF
            b3 =  hval        & 0xFF
            lines.append(
                f"{indent}pub const HASH_{rust_name}_HEX: [u8; 4] = "
                f"[0x{b0:02x}, 0x{b1:02x}, 0x{b2:02x}, 0x{b3:02x}];"
            )
        lines.append("")


def generate_header_rust(decls: list, source_name: str,
                          packed: bool = True) -> str:
    """Generate Rust source text from an IDL AST list."""
    reg   = _build_registry(decls)
    lines = []

    lines.append("// Auto-generated by idlc.py")
    lines.append(f"// Source: {source_name}")
    lines.append("#![allow(non_camel_case_types, dead_code)]")
    lines.append("")

    _emit_rust_declarations(lines, decls, reg, packed=packed)

    if lines and lines[-1] != "":
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Zenoh-pico serialize/deserialize helper code generation
# ---------------------------------------------------------------------------

def _build_cpp_qualified_map(decls: list, prefix: str = "") -> dict:
    """Build a {simple_name -> fully_qualified_cpp_name} dict from an IDL AST."""
    result = {}
    for d in decls:
        if isinstance(d, IDLModule):
            _result = _build_cpp_qualified_map(d.declarations, f"{prefix}{d.name}::")
            result.update(_result)
        elif isinstance(d, (IDLEnum, IDLBitmask, IDLStruct)):
            result[d.name] = f"{prefix}{d.name}"
    return result


def _ze_ser_c_field(lines: list, ft: str, fn: str, registry: dict, ind: str) -> None:
    """Emit zenoh serialize lines for one C struct field."""
    def _rc_check():
        lines.append(f"{ind}if (Z_OK != _rc)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    return _rc;")
        lines.append(f"{ind}}}")
        lines.append("")

    if ft in _IDL_ZE_PRIM:
        entry = _IDL_ZE_PRIM[ft]
        if entry is None:  # string
            lines.append(f"{ind}_rc = ze_serializer_serialize_str( _ser, _s->{fn} ? _s->{fn} : \"\" );")
        else:
            ser_fn, _, cast = entry
            lines.append(f"{ind}_rc = {ser_fn}( _ser, ({cast})_s->{fn} );")
        _rc_check()
        return
    node = registry.get(ft)
    if isinstance(node, IDLEnum):
        lines.append(f"{ind}_rc = ze_serializer_serialize_uint32( _ser, (uint32_t)_s->{fn} );")
        _rc_check()
    elif isinstance(node, IDLBitmask):
        c_type = _bitmask_underlying_c(node.bit_bound)
        ser_fn = _C_UINT_ZE[c_type][0]
        lines.append(f"{ind}_rc = {ser_fn}( _ser, ({c_type})_s->{fn} );")
        _rc_check()
    elif isinstance(node, IDLStruct):
        lines.append(f"{ind}_rc = {ft}_serialize( _ser, &_s->{fn} );")
        _rc_check()
    else:
        lines.append(f"{ind}/* TODO: unknown type '{ft}' field '{fn}' — skipped */")
        lines.append("")


def _ze_deser_c_field(lines: list, ft: str, fn: str, registry: dict, ind: str) -> None:
    """Emit zenoh deserialize lines for one C struct field."""
    def _rc_check_inner():
        lines.append(f"{ind}    if (Z_OK != _rc)")
        lines.append(f"{ind}    {{")
        lines.append(f"{ind}        return _rc;")
        lines.append(f"{ind}    }}")

    def _rc_check_outer():
        lines.append(f"{ind}if (Z_OK != _rc)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    return _rc;")
        lines.append(f"{ind}}}")
        lines.append("")

    if ft in _IDL_ZE_PRIM:
        entry = _IDL_ZE_PRIM[ft]
        if entry is None:  # string: heap-allocated; caller must manage lifetime
            lines.append(f"{ind}{{")
            lines.append(f"{ind}    z_owned_string_t  _zs_{fn};")
            lines.append(f"{ind}    _rc = ze_deserializer_deserialize_string( _deser, &_zs_{fn} );")
            _rc_check_inner()
            lines.append(f"{ind}    _s->{fn} = z_string_data( z_loan(_zs_{fn}) );")
            lines.append(f"{ind}    /* call z_drop(z_move(_zs_{fn})) when the string is no longer needed */")
            lines.append(f"{ind}}}")
            lines.append("")
        else:
            _, deser_fn, cast = entry
            lines.append(f"{ind}_rc = {deser_fn}( _deser, ({cast}*)&_s->{fn} );")
            _rc_check_outer()
        return
    node = registry.get(ft)
    if isinstance(node, IDLEnum):
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    uint32_t  _tmp_{fn};")
        lines.append(f"{ind}    _rc = ze_deserializer_deserialize_uint32( _deser, &_tmp_{fn} );")
        _rc_check_inner()
        lines.append(f"{ind}    _s->{fn} = ({ft})_tmp_{fn};")
        lines.append(f"{ind}}}")
        lines.append("")
    elif isinstance(node, IDLBitmask):
        c_type = _bitmask_underlying_c(node.bit_bound)
        deser_fn = _C_UINT_ZE[c_type][1]
        lines.append(f"{ind}_rc = {deser_fn}( _deser, ({c_type}*)&_s->{fn} );")
        _rc_check_outer()
    elif isinstance(node, IDLStruct):
        lines.append(f"{ind}_rc = {ft}_deserialize( _deser, &_s->{fn} );")
        _rc_check_outer()
    else:
        lines.append(f"{ind}/* TODO: unknown type '{ft}' field '{fn}' — skipped */")
        lines.append("")


def _emit_c_zenoh_helpers(lines: list, decls: list, registry: dict) -> None:
    """Emit static inline zenoh-pico serialize/deserialize helpers for every struct (C)."""
    flat = _collect_flat_decls(decls)
    structs = [d for d in flat if isinstance(d, IDLStruct)]
    sorted_structs = _topo_sort_structs(structs, registry)
    ind = "    "

    for s in sorted_structs:
        if not s.fields:
            continue
        sn = s.name

        p1_s  = "ze_loaned_serializer_t*"
        p2_s  = f"const {sn}*"
        tw_s  = max(len(p1_s), len(p2_s)) + 2
        pfx_s = f"static inline z_result_t {sn}_serialize("
        pad_s = " " * (len(pfx_s) + 1)

        lines.append(f"{pfx_s} {p1_s + ' ' * (tw_s - len(p1_s))}_ser,")
        lines.append(f"{pad_s}{p2_s + ' ' * (tw_s - len(p2_s))}_s )")
        lines.append("{")
        lines.append(f"{ind}z_result_t _rc;")
        lines.append("")
        for ft, fn in s.fields:
            _ze_ser_c_field(lines, ft, fn, registry, ind)
        lines.append(f"{ind}return Z_OK;")
        lines.append("}")
        lines.append("")

        p1_d  = "ze_deserializer_t*"
        p2_d  = f"{sn}*"
        tw_d  = max(len(p1_d), len(p2_d)) + 2
        pfx_d = f"static inline z_result_t {sn}_deserialize("
        pad_d = " " * (len(pfx_d) + 1)

        lines.append(f"{pfx_d} {p1_d + ' ' * (tw_d - len(p1_d))}_deser,")
        lines.append(f"{pad_d}{p2_d + ' ' * (tw_d - len(p2_d))}_s )")
        lines.append("{")
        lines.append(f"{ind}z_result_t _rc;")
        lines.append("")
        for ft, fn in s.fields:
            _ze_deser_c_field(lines, ft, fn, registry, ind)
        lines.append(f"{ind}return Z_OK;")
        lines.append("}")
        lines.append("")

        p1_tb = f"const {sn}*"
        p2_tb = "z_owned_bytes_t*"
        tw_tb = max(len(p1_tb), len(p2_tb)) + 2
        pfx_tb = f"static inline z_result_t {sn}_to_bytes("
        pad_tb = " " * (len(pfx_tb) + 1)

        lines.append(f"{pfx_tb} {p1_tb + ' ' * (tw_tb - len(p1_tb))}_s,")
        lines.append(f"{pad_tb}{p2_tb + ' ' * (tw_tb - len(p2_tb))}_out )")
        lines.append("{")
        lines.append(f"{ind}ze_owned_serializer_t _ser;")
        lines.append(f"{ind}z_result_t _rc = ze_serializer_empty(&_ser);")
        lines.append(f"{ind}if (Z_OK != _rc)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    return _rc;")
        lines.append(f"{ind}}}")
        lines.append("")
        lines.append(f"{ind}_rc = {sn}_serialize( z_loan_mut(_ser), _s );")
        lines.append(f"{ind}if (Z_OK != _rc)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    z_drop(z_move(_ser));")
        lines.append(f"{ind}    return _rc;")
        lines.append(f"{ind}}}")
        lines.append("")
        lines.append(f"{ind}ze_serializer_finish(z_move(_ser), _out);")
        lines.append(f"{ind}return Z_OK;")
        lines.append("}")
        lines.append("")

        p1_fb = "const z_loaned_bytes_t*"
        p2_fb = f"{sn}*"
        tw_fb = max(len(p1_fb), len(p2_fb)) + 2
        pfx_fb = f"static inline z_result_t {sn}_from_bytes("
        pad_fb = " " * (len(pfx_fb) + 1)

        lines.append(f"{pfx_fb} {p1_fb + ' ' * (tw_fb - len(p1_fb))}_bytes,")
        lines.append(f"{pad_fb}{p2_fb + ' ' * (tw_fb - len(p2_fb))}_s )")
        lines.append("{")
        lines.append(f"{ind}ze_deserializer_t _deser = ze_deserializer_from_bytes(_bytes);")
        lines.append(f"{ind}return {sn}_deserialize(&_deser, _s);")
        lines.append("}")
        lines.append("")


def _ze_ser_cpp_field(lines: list, ft: str, fn: str, registry: dict,
                       ind: str, qual_map: dict) -> None:
    """Emit zenoh C++ serialize lines for one struct field."""
    def _err_check():
        lines.append(f"{ind}if (_err && *_err != Z_OK)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    return false;")
        lines.append(f"{ind}}}")
        lines.append("")

    if ft in _IDL_ZE_PRIM:
        entry = _IDL_ZE_PRIM[ft]
        if entry is None:
            lines.append(f"{ind}_ser.serialize(_s.{fn}, _err);")
        else:
            _, _, cast = entry
            lines.append(f"{ind}_ser.serialize(static_cast<{cast}>(_s.{fn}), _err);")
        _err_check()
        return
    node = registry.get(ft)
    if isinstance(node, IDLEnum):
        lines.append(f"{ind}_ser.serialize(static_cast<uint32_t>(_s.{fn}), _err);")
        _err_check()
    elif isinstance(node, IDLBitmask):
        c_type = _bitmask_underlying(node.bit_bound)
        lines.append(f"{ind}_ser.serialize(static_cast<{c_type}>(_s.{fn}), _err);")
        _err_check()
    elif isinstance(node, IDLStruct):
        lines.append(f"{ind}{ft}_serialize(_ser, _s.{fn}, _err);")
        _err_check()
    else:
        lines.append(f"{ind}/* TODO: unknown type '{ft}' field '{fn}' — skipped */")
        lines.append("")


def _ze_deser_cpp_field(lines: list, ft: str, fn: str, registry: dict,
                         ind: str, qual_map: dict) -> None:
    """Emit zenoh C++ deserialize lines for one struct field."""
    def _err_check():
        lines.append(f"{ind}if (_err && *_err != Z_OK)")
        lines.append(f"{ind}{{")
        lines.append(f"{ind}    return false;")
        lines.append(f"{ind}}}")
        lines.append("")

    if ft in _IDL_ZE_PRIM:
        entry = _IDL_ZE_PRIM[ft]
        if entry is None:
            lines.append(f"{ind}_s.{fn} = _deser.deserialize<std::string>(_err);")
        else:
            _, _, cast = entry
            lines.append(f"{ind}_s.{fn} = static_cast<decltype(_s.{fn})>(_deser.deserialize<{cast}>(_err));")
        _err_check()
        return
    node = registry.get(ft)
    if isinstance(node, IDLEnum):
        qual = qual_map.get(ft, ft)
        lines.append(f"{ind}_s.{fn} = static_cast<{qual}>(_deser.deserialize<uint32_t>(_err));")
        _err_check()
    elif isinstance(node, IDLBitmask):
        c_type = _bitmask_underlying(node.bit_bound)
        lines.append(f"{ind}_s.{fn} = static_cast<decltype(_s.{fn})>(_deser.deserialize<{c_type}>(_err));")
        _err_check()
    elif isinstance(node, IDLStruct):
        lines.append(f"{ind}{ft}_deserialize(_deser, _s.{fn}, _err);")
        _err_check()
    else:
        lines.append(f"{ind}/* TODO: unknown type '{ft}' field '{fn}' — skipped */")
        lines.append("")


def _emit_cpp_zenoh_helpers(lines: list, decls: list, registry: dict) -> None:
    """Emit static inline zenoh C++ serialize/deserialize helpers for every struct."""
    qual_map = _build_cpp_qualified_map(decls)
    flat = _collect_flat_decls(decls)
    structs = [d for d in flat if isinstance(d, IDLStruct)]
    sorted_structs = _topo_sort_structs(structs, registry)
    ind = "    "

    for s in sorted_structs:
        if not s.fields:
            continue
        sn = s.name
        qual = qual_map.get(sn, sn)

        # --- serialize ---
        p1_s  = "zenoh::ext::Serializer&"
        p2_s  = f"const {qual}&"
        p3_s  = "ZResult*"
        tw_s  = max(len(p1_s), len(p2_s), len(p3_s)) + 2
        pfx_s = f"static inline bool {sn}_serialize("
        pad_s = " " * (len(pfx_s) + 1)

        lines.append(f"{pfx_s} {p1_s + ' ' * (tw_s - len(p1_s))}_ser,")
        lines.append(f"{pad_s}{p2_s + ' ' * (tw_s - len(p2_s))}_s,")
        lines.append(f"{pad_s}{p3_s + ' ' * (tw_s - len(p3_s))}_err = nullptr )")
        lines.append("{")
        for ft, fn in s.fields:
            _ze_ser_cpp_field(lines, ft, fn, registry, ind, qual_map)
        lines.append(f"{ind}return true;")
        lines.append("}")
        lines.append("")

        # --- deserialize ---
        p1_d  = "zenoh::ext::Deserializer&"
        p2_d  = f"{qual}&"
        p3_d  = "ZResult*"
        tw_d  = max(len(p1_d), len(p2_d), len(p3_d)) + 2
        pfx_d = f"static inline bool {sn}_deserialize("
        pad_d = " " * (len(pfx_d) + 1)

        lines.append(f"{pfx_d} {p1_d + ' ' * (tw_d - len(p1_d))}_deser,")
        lines.append(f"{pad_d}{p2_d + ' ' * (tw_d - len(p2_d))}_s,")
        lines.append(f"{pad_d}{p3_d + ' ' * (tw_d - len(p3_d))}_err = nullptr )")
        lines.append("{")
        for ft, fn in s.fields:
            _ze_deser_cpp_field(lines, ft, fn, registry, ind, qual_map)
        lines.append(f"{ind}return true;")
        lines.append("}")
        lines.append("")

        # --- to_bytes ---
        p1_tb = f"const {qual}&"
        p2_tb = "ZResult*"
        tw_tb = max(len(p1_tb), len(p2_tb)) + 2
        pfx_tb = f"static inline zenoh::Bytes {sn}_to_bytes("
        pad_tb = " " * (len(pfx_tb) + 1)

        lines.append(f"{pfx_tb} {p1_tb + ' ' * (tw_tb - len(p1_tb))}_s,")
        lines.append(f"{pad_tb}{p2_tb + ' ' * (tw_tb - len(p2_tb))}_err = nullptr )")
        lines.append("{")
        lines.append(f"{ind}zenoh::ext::Serializer _ser;")
        lines.append(f"{ind}{sn}_serialize(_ser, _s, _err);")
        lines.append(f"{ind}return std::move(_ser).finish();")
        lines.append("}")
        lines.append("")

        # --- from_bytes ---
        p1_fb = f"const zenoh::Bytes&"
        p2_fb = "ZResult*"
        tw_fb = max(len(p1_fb), len(p2_fb)) + 2
        pfx_fb = f"static inline {qual} {sn}_from_bytes("
        pad_fb = " " * (len(pfx_fb) + 1)

        lines.append(f"{pfx_fb} {p1_fb + ' ' * (tw_fb - len(p1_fb))}_bytes,")
        lines.append(f"{pad_fb}{p2_fb + ' ' * (tw_fb - len(p2_fb))}_err = nullptr )")
        lines.append("{")
        lines.append(f"{ind}zenoh::ext::Deserializer _deser(_bytes);")
        lines.append(f"{ind}{qual} _s{{}};")
        lines.append(f"{ind}{sn}_deserialize(_deser, _s, _err);")
        lines.append(f"{ind}return _s;")
        lines.append("}")
        lines.append("")


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

def parse_idl_file(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    tokens = _tokenize(text)
    return _Parser(tokens).parse()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"idlc.py v{APP_VERSION} - IDL header generator"
    )
    ap.add_argument(
        "--idl",
        required=True,
        metavar="FILE",
        help=".idl source file",
    )
    ap.add_argument(
        "--output",
        required=True,
        metavar="DIR",
        help="Output directory",
    )
    ap.add_argument(
        "--header-name",
        metavar="NAME",
        help="Output file name without extension (default: idl file name)",
    )
    ap.add_argument(
        "--language",
        choices=["cpp", "c", "csharp", "python", "rust"],
        default="cpp",
        help="Output language (default: cpp)",
    )
    ap.add_argument(
        "--zenoh",
        action="store_true",
        default=False,
        help="Generate zenoh-pico serialize/deserialize helpers for each struct (c and cpp only)",
    )
    # --packed / --no-packed (Python 3.9+ BooleanOptionalAction, fallback for older versions)
    try:
        ap.add_argument(
            "--packed",
            default=True,
            action=argparse.BooleanOptionalAction,
            help="Add packed attribute to structs (default: enabled)",
        )
    except AttributeError:
        ap.set_defaults(packed=True)
        _grp = ap.add_mutually_exclusive_group()
        _grp.add_argument("--packed",    dest="packed", action="store_true")
        _grp.add_argument("--no-packed", dest="packed", action="store_false")
    args = ap.parse_args()

    idl_path = os.path.abspath(args.idl)
    if not os.path.isfile(idl_path):
        print(f"ERROR: IDL file not found: {idl_path}", file=sys.stderr)
        return 1

    _ext_map = {"cpp": ".hpp", "c": ".h", "csharp": ".cs", "python": ".py", "rust": ".rs"}
    ext  = _ext_map[args.language]
    stem = os.path.splitext(os.path.basename(idl_path))[0]

    if args.header_name:
        base = args.header_name
        for e in _ext_map.values():
            if base.endswith(e):
                base = base[: -len(e)]
                break
        header_stem = base
    else:
        header_stem = stem

    out_dir  = os.path.abspath(args.output)
    out_path = os.path.join(out_dir, header_stem + ext)

    try:
        decls = parse_idl_file(idl_path)
    except SyntaxError as e:
        print(f"ERROR: IDL parse error: {e}", file=sys.stderr)
        return 1

    if args.language == "cpp":
        content = generate_header(decls, stem, header_stem, packed=args.packed, zenoh=args.zenoh)
    elif args.language == "c":
        content = generate_header_c(decls, stem, header_stem, packed=args.packed, zenoh=args.zenoh)
    elif args.language == "csharp":
        content = generate_header_csharp(decls, stem, packed=args.packed)
    elif args.language == "python":
        content = generate_header_python(decls, stem, packed=args.packed)
    else:  # rust
        content = generate_header_rust(decls, stem, packed=args.packed)

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[idlc.py] Generated: {os.path.relpath(out_path)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
