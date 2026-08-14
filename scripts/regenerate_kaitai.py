#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
"""Regenerate acd/generated/ from the Kaitai Struct (.ksy) templates in
resources/templates/. Requires the Kaitai Struct compiler
(kaitai-struct-compiler.bat on Windows, ksc elsewhere) on PATH -- a
separate, external (Java-based) tool this project does not vendor or
install; see https://kaitai.io/#download.

This is a MAINTAINER-ONLY step, run explicitly when you've changed a .ksy
template -- NOT part of `pip install` (it used to be, via a custom
`install` command in setup.py; that made every install, including a plain
`pip install .`/`pip install git+...` from a machine without the Kaitai
compiler on PATH, fail outright, even though acd/generated/'s own output
is already committed to git and doesn't need regenerating for a normal
consumer install). Run this manually after editing a template:

    python scripts/regenerate_kaitai.py
"""
import platform
import subprocess
import sys

_COMPILER = "kaitai-struct-compiler.bat" if platform.system() == "Windows" else "ksc"

# (template path, --outdir, --python-package) -- same set setup.py's old
# install-time hook compiled. Not necessarily a complete list of every
# template acd/generated/'s current content was built from (some of the
# currently-committed generated files, e.g. controller/map_device/tag
# parsers, don't have a corresponding entry here) -- extend this list the
# next time one of those needs regenerating too, rather than assuming this
# is exhaustive.
_TEMPLATES = [
    ("resources/templates/Dat/Dat.ksy", "acd/generated/", "acd.generated"),
    ("resources/templates/Comps/FAFA_Comps.ksy", "acd/generated/comps/", "acd.generated.comps"),
    ("resources/templates/SbRegion/FAFA_SbRegion.ksy", "acd/generated/sbregion/", "acd.generated.sbregion"),
    ("resources/templates/Comps/FDFD_Comps.ksy", "acd/generated/comps/", "acd.generated.comps"),
    ("resources/templates/Comments/FAFA_Comments.ksy", "acd/generated/comments/", "acd.generated.comments"),
    ("resources/templates/Comps/RxGeneric.ksy", "acd/generated/comps/", "acd.generated.comps"),
]


def main() -> int:
    for template, outdir, package in _TEMPLATES:
        print(f"Compiling {template} -> {outdir}")
        result = subprocess.run(
            [_COMPILER, "-t", "python", "--outdir", outdir, "--python-package", package, template]
        )
        if result.returncode != 0:
            print(f"kaitai-struct-compiler failed on {template} (exit {result.returncode})",
                  file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
