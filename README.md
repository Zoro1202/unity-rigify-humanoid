# Unity Rigify Humanoid Converter

A Windows Unity Editor package that converts supported Blender Rigify FBXs into validated Unity Humanoid prefabs. MIT licensed. No character assets or Blender binaries are included.

## Requirements
- Windows, Unity **6000.3** (validated on 6000.3.9f1).
- An installed Blender executable (validated with 5.2.3 LTS).
- A static conventional human Rigify FBX imported under your project's Assets folder.

## Install
Unity: **Window > Package Manager > + > Install package from disk**, select this repository's `package.json`.
For Git installation, paste `https://github.com/Zoro1202/unity-rigify-humanoid.git` into **Install package from git URL**. Python scripts are resolved from the installed package, including PackageCache; no project Tools folder is needed.

## Use
1. Select the source FBX in the Project window.
2. Open **Tools > Rigify Humanoid > Converter**.
3. Confirm the Blender executable, then click **Convert**.
4. Select the generated prefab under `Assets/RigifyGenerated/<source>_<unique-id>/`.

원본 FBX 선택 → **Tools > Rigify Humanoid > Converter** → **Convert**.
원본과 기존 결과는 덮어쓰지 않고 매번 새 결과를 생성합니다.

## Supported profile and limitations
One human armature with root, conventional DEF limb names and a single recognizable DEF-spine chain; one or more uniquely named skinned meshes. Shoulder/toe pairs are optional. All DEF bones and root are retained, including unweighted optional finger/face bones; individual finger/face Humanoid mapping is not generated.

Unknown/ambiguous layouts, weighted controls, animations, constraints, drivers, sockets, shape keys, extra scene objects, other modifiers and envelope skinning are rejected rather than silently stripped. This is **not a universal Rigify converter**. Export model+armature only, without baked animation, from a conversion copy; keep your authored original.

Source hashes, mesh topology/vertices/UVs/material slots/weights/rest pose, and full bone names/parents are validated through Blender export/reimport. Unity checks the report, source/output hashes, mapping, Avatar and skinned geometry. Original material references are preserved, so moving the result requires its material/texture dependencies.

No gameplay character replacement or controller assignment. Humanoid validity is not proof that every external animation, Foot IK or root-motion combination is correct. Check converted assets visually before production use.

Cancellation, window close, assembly reload and shutdown stop the owned Blender job using a Windows kill-on-close job object. A 300-second timeout applies. Failed import rolls back only the new output folder. Forced interruption may leave hidden staging directories under Library/RigifyHumanoid.

## Tests
Enable `com.zoro122.rigify-humanoid` in the project's manifest `testables` array and run EditMode tests under namespace `RigifyHumanoid.Tests` with Unity Test Framework installed. Native window visual layout is not tested by headless tests.

**Maintainer-only Blender reference-profile regression suite**, not a generic user acceptance test: it requires a single skinned mesh, exactly seven axial DEF-spine bones, a DEF-breast.L parented to DEF-spine.003, and conventional limbs. Arbitrary supported inputs can legitimately fail these fixture-specific assertions. No reference model is distributed. Supply your own matching fixtures only if maintaining this test profile:
```text
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python Tests/Blender/test_prepare_rigify_humanoid.py -- SOURCE_FBX ABSOLUTE_EVIDENCE_DIRECTORY REQUESTED_SOURCE_FBX
```
The two reference-profile source paths may be the same. Use an absolute scratch evidence directory: the suite replaces its own final-fixtures/final-converted children. Do not point it at valuable data. Generated variants in this suite are derived fixtures, not independently authored characters.

## License and provenance
MIT applies to this repository's tool code only, not Blender, Unity, or user-imported assets. Blender is an external prerequisite, not redistributed. FBX utilities originated in the author's earlier character preparation tool and are retained here to preserve validated numerical behavior; their legacy specialized CLI is not the supported public entry point. No third-party character, animation, texture, or game data is included.
