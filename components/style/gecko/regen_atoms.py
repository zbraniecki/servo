#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import re
import os
import sys

from io import BytesIO

GECKO_DIR = os.path.dirname(__file__.replace('\\', '/'))
sys.path.insert(0, os.path.join(os.path.dirname(GECKO_DIR), "properties"))

import build


# Matches lines like `GK_ATOM(foo, "foo", 0x12345678, nsStaticAtom, PseudoElementAtom)`.
PATTERN = re.compile('^GK_ATOM\(([^,]*),[^"]*"([^"]*)",\s*(0x[0-9a-f]+),\s*([^,]*),\s*([^)]*)\)',
                     re.MULTILINE)
FILE = "include/nsGkAtomList.h"


def map_atom(ident):
    if ident in {"box", "loop", "match", "mod", "ref",
                 "self", "type", "use", "where", "in"}:
        return ident + "_"
    return ident


class Atom:
    def __init__(self, ident, value, hash, ty, atom_type):
        self.ident = "nsGkAtoms_{}".format(ident)
        self.original_ident = ident
        self.value = value
        self.hash = hash
        # The Gecko type: "nsStaticAtom", "nsICSSPseudoElement", or "nsIAnonBoxPseudo"
        self.ty = ty
        # The type of atom: "Atom", "PseudoElement", "NonInheritingAnonBox",
        # or "InheritingAnonBox"
        self.atom_type = atom_type
        if self.is_pseudo() or self.is_anon_box():
            self.pseudo_ident = (ident.split("_", 1))[1]
        if self.is_anon_box():
            assert self.is_inheriting_anon_box() or self.is_non_inheriting_anon_box()

    def type(self):
        return self.ty

    def capitalized_pseudo(self):
        return self.pseudo_ident[0].upper() + self.pseudo_ident[1:]

    def is_pseudo(self):
        return self.atom_type == "PseudoElementAtom"

    def is_anon_box(self):
        return self.is_non_inheriting_anon_box() or self.is_inheriting_anon_box()

    def is_non_inheriting_anon_box(self):
        return self.atom_type == "NonInheritingAnonBoxAtom"

    def is_inheriting_anon_box(self):
        return self.atom_type == "InheritingAnonBoxAtom"

    def is_tree_pseudo_element(self):
        return self.value.startswith(":-moz-tree-")


def collect_atoms(objdir):
    atoms = []
    path = os.path.abspath(os.path.join(objdir, FILE))
    print("cargo:rerun-if-changed={}".format(path))
    with open(path) as f:
        content = f.read()
        for result in PATTERN.finditer(content):
            atoms.append(Atom(result.group(1), result.group(2), result.group(3),
                              result.group(4), result.group(5)))
    return atoms


class FileAvoidWrite(BytesIO):
    """File-like object that buffers output and only writes if content changed."""
    def __init__(self, filename):
        BytesIO.__init__(self)
        self.name = filename

    def write(self, buf):
        if isinstance(buf, unicode):
            buf = buf.encode('utf-8')
        BytesIO.write(self, buf)

    def close(self):
        buf = self.getvalue()
        BytesIO.close(self)
        try:
            with open(self.name, 'rb') as f:
                old_content = f.read()
                if old_content == buf:
                    print("{} is not changed, skip".format(self.name))
                    return
        except IOError:
            pass
        with open(self.name, 'wb') as f:
            f.write(buf)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if not self.closed:
            self.close()


PRELUDE = '''
/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

// Autogenerated file created by components/style/gecko/regen_atoms.py.
// DO NOT EDIT DIRECTLY
'''[1:]

IMPORTS = '''
use gecko_bindings::structs::nsStaticAtom;
use string_cache::Atom;
'''

UNSAFE_STATIC = '''
#[inline(always)]
pub unsafe fn atom_from_static(ptr: *const nsStaticAtom) -> Atom {
    Atom::from_static(ptr)
}
'''

SATOMS_TEMPLATE = '''
            #[link_name = \"{link_name}\"]
            pub static nsGkAtoms_sAtoms: *const nsStaticAtom;
'''[1:]

CFG_IF_TEMPLATE = '''
cfg_if! {{
    if #[cfg(not(target_env = "msvc"))] {{
        extern {{
{gnu}\
        }}
    }} else if #[cfg(target_pointer_width = "64")] {{
        extern {{
{msvc64}\
        }}
    }} else {{
        extern {{
{msvc32}\
        }}
    }}
}}\n
'''

CONST_TEMPLATE = '''
pub const k_{name}: isize = {index};
'''[1:]

RULE_TEMPLATE = '''
("{atom}") =>
    {{{{
        use $crate::string_cache::atom_macro;
        #[allow(unsafe_code)] #[allow(unused_unsafe)]
        unsafe {{ atom_macro::atom_from_static(atom_macro::nsGkAtoms_sAtoms.offset(atom_macro::k_{name})) }}
    }}}};
'''[1:]

MACRO_TEMPLATE = '''
#[macro_export]
macro_rules! atom {{
{body}\
}}
'''

def write_atom_macro(atoms, file_name):
    with FileAvoidWrite(file_name) as f:
        f.write(PRELUDE)
        f.write(IMPORTS)
        f.write(UNSAFE_STATIC)

        gnu_name='_ZN9nsGkAtoms6sAtomsE'
        gnu_symbols = SATOMS_TEMPLATE.format(link_name=gnu_name)

        # Prepend "\x01" to avoid LLVM prefixing the mangled name with "_".
        # See https://github.com/rust-lang/rust/issues/36097
        msvc32_name = '\\x01?sAtoms@nsGkAtoms@@0QBVnsStaticAtom@@B'
        msvc32_symbols = SATOMS_TEMPLATE.format(link_name=msvc32_name)

        msvc64_name = '?sAtoms@nsGkAtoms@@0QEBVnsStaticAtom@@EB'
        msvc64_symbols = SATOMS_TEMPLATE.format(link_name=msvc64_name)

        f.write(CFG_IF_TEMPLATE.format(gnu=gnu_symbols, msvc32=msvc32_symbols, msvc64=msvc64_symbols))

        consts = [CONST_TEMPLATE.format(name=atom.ident, index=i) for (i, atom) in enumerate(atoms)]
        f.write('{}'.format(''.join(consts)))

        macro_rules = [RULE_TEMPLATE.format(atom=atom.value, name=atom.ident) for atom in atoms]
        f.write(MACRO_TEMPLATE.format(body=''.join(macro_rules)))


def write_pseudo_elements(atoms, target_filename):
    pseudos = []
    for atom in atoms:
        if atom.type() == "nsICSSPseudoElement" or atom.type() == "nsICSSAnonBoxPseudo":
            pseudos.append(atom)

    pseudo_definition_template = os.path.join(GECKO_DIR, "pseudo_element_definition.mako.rs")
    print("cargo:rerun-if-changed={}".format(pseudo_definition_template))
    contents = build.render(pseudo_definition_template, PSEUDOS=pseudos)

    with FileAvoidWrite(target_filename) as f:
        f.write(contents)


def generate_atoms(dist, out):
    atoms = collect_atoms(dist)
    write_atom_macro(atoms, os.path.join(out, "atom_macro.rs"))
    write_pseudo_elements(atoms, os.path.join(out, "pseudo_element_definition.rs"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: {} dist out".format(sys.argv[0]))
        exit(2)
    generate_atoms(sys.argv[1], sys.argv[2])
