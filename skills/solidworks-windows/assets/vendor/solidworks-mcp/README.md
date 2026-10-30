# SolidWorks MCP Server

Drive SolidWorks from an MCP client (Claude Code, Claude Desktop, any MCP host) through
its COM API. **Pure Python — no `SolidWorks.Interop.*` assemblies, no .NET service, no
generated type-library cache.** One install drives SolidWorks **2018 through 2026** —
**directly verified, tool by tool, against a live SolidWorks 2023 install**; every other
supported year runs on the same code path and is checked automatically at connect time
(see [Version support](#version-support)).

41 tools covering parts, assemblies, drawings, sheet metal, analysis and export.

---

## Why this exists

This project was built by combining two earlier SolidWorks MCP servers, each of which
had one half of the answer:

| | `mcp-server-solidworks` | `Solidworks-MCP` | **this server** |
|---|---|---|---|
| Tool breadth | Broad — sheet metal, drawings, assemblies, mates, index-based selection | Thin — basic sketch/extrude/fillet only | **Broad, deduplicated** |
| Implementation | C# .NET service + Python HTTP adapter | Pure Python `win32com` | **Pure Python `win32com`** |
| Version support | Narrow — compiled against one pinned interop assembly | Wide — late binding | **Wide, and verified at runtime** |
| Threading | STA (correct) | MTA — silently breaks the advanced APIs | **STA** |
| Units | Metres only | Millimetres only | **Any, per call** |
| Enum constants | Pinned interop assembly | Hardcoded ints | **Read from the install's own type library** |

The two weaknesses were causally linked. The C# project needed a compiled interop
assembly because that is how C# reaches COM — which is exactly why its version support
was narrow. The Python project had wide version support because late binding is
version-agnostic — but it lacked sheet metal, drawings and assemblies, because those
APIs *fail silently* from an MTA thread, which is what plain `win32com.Dispatch` gives
you.

So the interesting result is that you can have both, and four specific mechanisms are
what make it work.

### 1. Every COM call runs on one STA thread

SolidWorks is an apartment-threaded COM server. Called from an MTA thread, simple
property reads marshal fine — which is why a basic extrude-and-fillet server appears to
work. What does *not* survive marshalling is precisely the advanced surface:

- `IFeatureManager.CreateDefinition` / `CreateFeature` — sheet-metal base flange, shell
- `AccessSelections` / `ModifyDefinition` — edge-flange length, sketched-bend direction
- selection state that must persist across several calls

These return `null` with no error from an MTA caller, which reads as "the API is broken"
rather than "the apartment is wrong."

[`com/connection.py`](src/solidworks_mcp/com/connection.py) owns a single thread
initialised with `COINIT_APARTMENTTHREADED`, and every COM touch in the server is
marshalled onto it. COM objects never cross threads. That one decision is what lets a
pure-Python implementation reach the feature set the C# project needed a compiled
service for. It is also the natural place to enforce a call timeout, so a modal
SolidWorks dialog blocks one tool call instead of wedging the server.

### 2. Signature churn is absorbed at runtime, not at build time

SolidWorks revs feature APIs by suffixing a number (`FeatureExtrusion2` → `3`,
`FeatureCut3` → `4`, `AddMate3` → `5`) *and* by appending parameters to existing methods
between releases. A single hard-coded argument vector only works on the release it was
written against.

[`compat.invoke_first`](src/solidworks_mcp/com/compat.py) takes an ordered candidate
list and returns the first attempt that actually produces a result:

```python
report = compat.invoke_first(manager, [
    ("FeatureExtrusion3", args_23),   # SolidWorks 2014+
    ("FeatureExtrusion2", args_23),   # older, parameter added mid-generation
    ("FeatureExtrusion2", args_20),   # older still
])
```

Older releases do not expose the newer method and fall through; a mid-generation
parameter addition is covered by listing both vectors for the same name. Every result
reports which signature won (`"api": "FeatureExtrusion3/23"`), and a total failure
reports *every* signature tried and how each failed — so a failure on an unusual build
is debuggable rather than mysterious.

Success is tracked explicitly rather than inferred from the return value, because
void-returning APIs like `InsertRib` succeed *with* a `None` result while
`InsertAxis2` fails with a `False` one. Three predicates cover the cases:
`invoke_first` (null means failure), `invoke_bool` (false means failure), `invoke_any`
(void, caller verifies the effect).

### 3. Property-vs-method binding is resolved once, centrally

Under `win32com` dynamic dispatch, zero-argument accessors like `FirstFeature`,
`GetNextFeature`, `GetTypeName2` and `GetTitle` bind as *properties* on some SolidWorks
builds and *methods* on others. Both reference projects hit this and scattered ad-hoc
`try/except` pairs across every call site. Here it is one helper:

```python
compat.pm(doc, "GetTitle")          # works either way
compat.pm(feature, "IsSuppressed", default=False)
```

### 4. Enum constants come from the installation, not from memory

`swconst.tlb` ships next to `SLDWORKS.exe` in every install, so it can be loaded **by
path** — no GUID, no registry walk, no code generation, no version pinning — and its
enumerations read directly. [`com/constants.py`](src/solidworks_mcp/com/constants.py)
does this once and caches the result, giving the value *this* SolidWorks actually uses.

Hand-maintained fallbacks live in [`com/enums.py`](src/solidworks_mcp/com/enums.py) for
when the type library cannot be read. Where a wrong constant would be *actively harmful*
rather than merely ineffective — the user-preference toggles, which mutate SolidWorks
settings — the operation is **skipped unless the value was verified**, because flipping
an unrelated preference is worse than not applying the optimisation.
`get_solidworks_info` reports which mode you are in.

---

## Version support

This server has been directly, tool-by-tool tested against a live **SolidWorks 2023**
install. It also runs on every year from **2018 to 2026** without code changes, because
the COM interfaces it calls have been stable across that whole range and — per the enum
mechanism above — every constant is re-read from *that install's own* `swconst.tlb` at
connect time rather than assumed from a build-time guess. A different version whose
numeric constants happen to differ from 2023's still gets its own correct values, not
this server's fallbacks.

`connect_solidworks` and `get_solidworks_info` both report `version_support`, one of:

| `level` | Meaning |
|---|---|
| `verified` | This exact year has been directly tested (currently: 2023 only). |
| `compatible` | An untested year, but its own type library confirmed every constant this server uses — expected to behave like a tested version. |
| `degraded` | A recognized year, but its type library could not be read — falling back to another version's hardcoded constants. Preference toggles and some feature calls may silently use the wrong value. |
| `unsupported` | The `RevisionNumber` did not map to any known marketing year at all — a release older or newer than this server has ever seen. It may still work if the underlying COM interfaces are unchanged, but nothing is checked. |

Only `verified` and `compatible` are silent; `degraded` and `unsupported` add a
`WARNING:` line to the tool's own message so it surfaces without a separate call. The
decision logic lives in [`enums.version_support`](src/solidworks_mcp/com/enums.py) and
is unit-tested directly (`TestVersionSupport` in `tests/test_offline.py`).

To mark a new year as directly tested after running the tool suite against it, add it to
`enums.VERIFIED_VERSIONS`.

---

## Selection: prefer indices over coordinates

This is the single most important thing to know when using the server.

`SelectByID2` with coordinates picks *the nearest* entity within a tolerance. That is
fine for a lone face on a cube and unusable for a concave inner corner, a small-radius
fillet edge, or a revolve end-cap where several faces meet near one point.

So the analysis tools hand out **stable indices**, and the feature tools consume them:

```
analyze_model(analysis_type="faces")   →  [{i: 0, planar: true, normal: [...], ...}, ...]
create_sketch(face_index=3)
extrude_feature(up_to_face_index=7)

analyze_model(analysis_type="edges")   →  [{i: 0, start: [...], mid: [...], ...}, ...]
add_edge_feature("fillet", 2, edge_indices=[3, 5])
```

The enumeration behind the reporting and the selection is *the same code*
([`com/selection.py`](src/solidworks_mcp/com/selection.py)), so an index always resolves
to the entity you meant. Coordinate picking remains available as a fallback.

`get_selection` closes the loop from the other direction: when the user clicks geometry
in the SolidWorks window while describing what they want, it maps their click back onto
those same indices.

On a large import, narrow the result instead of dumping thousands of entities:

```
analyze_model(analysis_type="faces", near=[0.05, 0, 0.02], limit=5)
analyze_model(analysis_type="edges", axis=[0, 0, 1])     # only edges along Z
```

---

## Install

Requires Windows, Python 3.10+, and a local SolidWorks installation.

```bash
git clone <this-repo> C:/solidworks-mcp
cd C:/solidworks-mcp
pip install -e .
```

Register it with your MCP client:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "solidworks-mcp"
    }
  }
}
```

Or point at the checkout without installing:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:/solidworks-mcp/run_server.py"]
    }
  }
}
```

Verify:

```bash
python -c "import sys; sys.path.insert(0,'src'); from solidworks_mcp.server import mcp; print('ok')"
```

---

## Units

Lengths default to **millimetres**; every tool takes an optional `unit`
(`mm`, `cm`, `m`, `um`, `in`, `ft`, plus aliases like `inch`). Angles are **always in
degrees**, consistently — the reference projects mixed degrees and radians between tools.

Internally everything is SI, and analysis results report both:

```json
{ "meters": 0.025, "value": 25.0, "unit": "mm" }
```

`set_units("in")` changes the default for the session. An unrecognised unit is a hard
error, not a silent fallback to millimetres — a typo should not become wrong geometry.

---

## Tools

### Connection
| Tool | Purpose |
|---|---|
| `connect_solidworks` | Connect, launching SolidWorks if needed. Reports version and probed API capabilities. Optional — every tool connects automatically. |
| `get_solidworks_info` | Installations, version, capability probe, constant-verification status, effective config. |
| `set_units` | Default length unit for the session. |

### Documents
| Tool | Purpose |
|---|---|
| `new_document` | Blank part, assembly or drawing. Template auto-resolved. |
| `open_document` | Open native or foreign files. Three-stage import escalation for STEP/IGES/NX/CATIA/Creo. |
| `save_document` | Save, or Save As with directory creation. |
| `close_document` | Close one document, or all (including invisibly-loaded components). |
| `activate_document` | Switch to an open document by title. |
| `list_documents` | Open documents, active flag, unsaved-changes flag. |
| `rebuild_document` | Rebuild and report feature errors. |
| `export_document` | STEP, IGES, STL, Parasolid, 3MF, OBJ, PDF, DWG, DXF, JPG, PNG — one or many at once. |
| `set_configuration` | Activate a named configuration. |

### Sketching
| Tool | Purpose |
|---|---|
| `create_sketch` | New sketch on a plane or a model face (by index or coordinate). |
| `edit_sketch` | Reopen an existing sketch. |
| `exit_sketch` | Close the active sketch without consuming it. |
| `add_sketch_entities` | **Batch** — line, centerline, rectangle, center_rectangle, circle, arc, arc_center, ellipse, spline, polygon, slot, point, fillet, chamfer. Any record can be construction geometry. |
| `add_sketch_constraint` | horizontal, vertical, fix, coincident, parallel, perpendicular, tangent, equal, concentric, collinear, midpoint, symmetric. |
| `add_sketch_dimension` | Driving dimension on the nearest segment. Reports whether the sketch is now fully defined. |

### Features
| Tool | Purpose |
|---|---|
| `extrude_feature` | boss, cut, revolve, sweep, loft, thin. Blind / through-all / mid-plane / up-to-face. Auto-retries a failed cut in the opposite direction. |
| `add_edge_feature` | fillet, or chamfer (distance-angle, distance-distance, equal-distance). |
| `create_rib` | Rib from an open profile. Auto-retries the flipped material direction. |
| `create_shell` | Hollow to a uniform wall, optionally opening through faces. |
| `create_hole` | Round hole on a face in one call; stays a recognisable Hole feature for drawing callouts. |
| `add_reference_geometry` | Plane (signed offset), axis, point. |
| `create_pattern` | linear, circular, mirror. |
| `set_material` | Assign a library material. |
| `modify_dimension` | Drive a named dimension. Reads the effective value back and flags clamping. |
| `edit_feature` | suppress, unsuppress, delete, rename, rollback_to, rollback_end. |

### Sheet metal
`sheet_metal_feature` — `base_flange`, `edge_flange`, `edge_flange_sketch` /
`edge_flange_finish` (the two-step custom-profile flow), `sketched_bend`,
`flat_pattern`, `fold`.

### Drawings
| Tool | Purpose |
|---|---|
| `create_drawing_view` | Standard projections, `flat_pattern` (the correct way to detail sheet metal), and `section` views. |
| `annotate_drawing` | `model_dimensions` (robust — no coordinate picking), `center_marks`, `hole_callout`, `dimension`, `note`. |
| `analyze_drawing` | Views, dimensions, optionally projected 2D geometry. |

### Assemblies
| Tool | Purpose |
|---|---|
| `insert_component` | Insert with position or a full 13-number transform; explicit fixed state. |
| `add_mate` | Index-based mating. Returns both components' resulting transforms. |
| `analyze_assembly` | components, components_flat, mates, faces, edges. |
| `save_body_as_part` | Extract one body of a multibody part — the missing half of flattened-assembly reconstruction. |

### Analysis
| Tool | Purpose |
|---|---|
| `analyze_model` | summary, mass_properties, geometry, bodies, faces, edges, features, sketch, configurations. |
| `get_selection` | What the user has selected, mapped to stable indices. |
| `compare_documents` | Objective two-document diff with a pass/fail verdict. |

### Scripting
| Tool | Purpose |
|---|---|
| `execute_python` | Arbitrary Python against live COM, on the STA thread, with the server's own helpers in scope. |
| `run_macro` | Run an existing `.swp` / `.swb` / `.dll` macro. |

---

## Result format

Every tool returns the same JSON envelope, so an agent can branch on `ok` without
parsing prose:

```json
{
  "ok": true,
  "op": "extrude_feature",
  "message": "Created Boss-Extrude1 (boss). Body now has 6 faces, 12 edges.",
  "data": {
    "feature": "Boss-Extrude1",
    "api": "FeatureExtrusion3/23",
    "body": { "bodies": 1, "faces": 6, "edges": 12, "vertices": 8, "volume": 0.000125 }
  },
  "state": { "document": "Part1", "type": "PART", "active_sketch": null }
}
```

Failures carry a stable code and — importantly — a hint naming the next action, so an
agent can usually recover without asking the user:

```json
{
  "ok": false,
  "op": "add_edge_feature",
  "error": {
    "code": "EDGE_NOT_FOUND",
    "message": "edge_index 14 is out of range: the part has 12 edges (0-11).",
    "hint": "Call analyze_model(analysis_type='edges') for valid indices."
  }
}
```

Geometry-changing tools return a body summary in-band, so a caller can confirm a feature
landed without a separate analysis round-trip. When output is capped, the result says so
explicitly — a silently truncated list reads as "this is everything".

---

## Example: a bracket, start to finish

```
new_document(doc_type="part")

create_sketch(plane="Front Plane")
add_sketch_entities(entities=[
  {"entity_type": "rectangle", "x1": 0, "y1": 0, "x2": 80, "y2": 60}
])
extrude_feature(feature_type="boss", depth=10)

analyze_model(analysis_type="faces")           # → pick the top face, say i=1
create_sketch(face_index=1)
add_sketch_entities(entities=[
  {"entity_type": "circle", "cx": 20, "cy": 20, "radius": 4},
  {"entity_type": "circle", "cx": 60, "cy": 40, "radius": 4}
])
extrude_feature(feature_type="cut", through_all=True)

analyze_model(analysis_type="edges", axis=[0, 0, 1])   # → vertical edges only
add_edge_feature(feature_type="fillet", size=5, edge_indices=[2, 5, 8, 11])

set_material(material="1060 Alloy")
save_document(file_path="C:/parts/bracket.sldprt")

new_document(doc_type="drawing")
create_drawing_view(view_type="front", position=[80, 200])
create_drawing_view(view_type="isometric", position=[280, 200], scale=0.5)
annotate_drawing(action="model_dimensions")
annotate_drawing(action="center_marks")
export_document(formats=["PDF", "DWG"], output_path="C:/parts/bracket")
```

---

## Configuration

Defaults are sensible; override via `SOLIDWORKS_*` environment variables or a
`solidworks_mcp.json` next to the package (env wins).

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `exe_path` | `SOLIDWORKS_EXE` | auto-detect | Registry-enumerated, newest first. |
| `auto_launch` | `SOLIDWORKS_AUTO_LAUNCH` | `true` | |
| `visible` | `SOLIDWORKS_VISIBLE` | `true` | Leave on: an invisible instance with a modal dialog is unrecoverable. |
| `startup_timeout` | `SOLIDWORKS_STARTUP_TIMEOUT` | `180` | Seconds to wait for a cold start. |
| `call_timeout` | `SOLIDWORKS_CALL_TIMEOUT` | `300` | Exceeding it almost always means an open dialog. |
| `default_unit` | `SOLIDWORKS_DEFAULT_UNIT` | `mm` | |
| `allow_execute_python` | `SOLIDWORKS_ALLOW_EXEC` | `true` | **Set false for shared deployments** — it grants arbitrary code execution. |
| `max_entities` | `SOLIDWORKS_MAX_ENTITIES` | `2000` | Caps face/edge dumps. |
| `log_level` | `SOLIDWORKS_LOG_LEVEL` | `INFO` | |

Logging goes to a rotating file only — stdout is the MCP transport and must stay clean.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q      # 126 offline tests, no SolidWorks needed
python -m ruff check src/ tests/
```

The offline suite covers unit conversion, the result envelope, argument coercion, the
enum tables and the compatibility layer's dispatch logic — the last exercised against
fake COM objects that reproduce the real failure modes: a property where a method was
expected, an arity mismatch, a null return, a cyclic feature tree.

```
src/solidworks_mcp/
├── server.py            MCP tool definitions and dispatch
├── config.py            Settings resolution
├── com/
│   ├── connection.py    STA thread, connection, capability probing
│   ├── compat.py        Property-vs-method, arity-tolerant invocation, tree walks
│   ├── selection.py     Index-based selection, topology enumeration, geometry readers
│   ├── constants.py     Enum values read from the install's type library
│   ├── enums.py         Hand-maintained constant fallbacks
│   └── finder.py        Installation and template discovery
├── ops/                 documents, sketches, features, sheetmetal, drawings,
│                        assemblies, analysis, scripting
└── util/                units, result envelope
```

---

## Known limitations

- **Windows only**, and requires a local SolidWorks installation. The COM API has no
  remote or headless mode.
- **A modal dialog blocks the call it lands in.** The server suppresses the
  "enter dimension value" popup where it can verify the toggle index, but a license
  prompt or rebuild-error dialog will time out the current tool call. The worker
  survives; dismiss the dialog and retry.
- **Coordinate selection is inherently ambiguous.** Where a tool offers both, the
  index path is the supported one; coordinates are a convenience.
- **Sheet-metal and drawing APIs vary most between releases.** These are the tools most
  likely to need a new candidate signature on an unusual build — the failure message
  names every signature tried, which is the information needed to add one.
- **CAD operations are not transactional.** A batch of sketch entities that fails
  partway leaves the earlier entities in place; the error reports how many were created.
- **Second-direction linear patterns** are passed through to the API but the direction-2
  reference entity is not wired, so `count2` may not take effect on all builds. Compose
  a grid as a pattern of a pattern instead.
- Live COM behaviour has been directly exercised only against SolidWorks 2023; other
  supported years (2018-2026) rely on stable COM interfaces plus runtime constant
  verification rather than direct testing. The compatibility layer is designed to
  degrade with a clear diagnostic rather than silently — see
  [Version support](#version-support) and `get_solidworks_info`'s `version_support` field.

## License

MIT
