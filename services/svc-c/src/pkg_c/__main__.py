import json
from importlib.metadata import version

import idna
from pkg_b import shout

print(shout("svc-c"))
print(json.dumps({d: version(d) for d in ("pkg-a", "pkg-b", "pkg-c", "six", "idna")}))
