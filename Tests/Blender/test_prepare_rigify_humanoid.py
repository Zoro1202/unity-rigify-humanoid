"""Maintainer seven-spine reference-profile Blender regression tests.

Not a generic acceptance suite for arbitrary supported FBXs: requires one mesh,
seven axial DEF-spine bones, DEF-breast.L, and the conventional limb hierarchy.

Run with Blender in background mode; arguments after ``--`` are:
SOURCE_FBX EVIDENCE_DIRECTORY REQUESTED_SOURCE_FBX.
"""

from __future__ import annotations

import importlib.util
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import bmesh
import bpy


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "Editor" / "Blender" / "prepare_rigify_humanoid.py"


def load_subject():
    spec = importlib.util.spec_from_file_location("prepare_rigify_humanoid", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blender_args() -> tuple[Path, Path, Path]:
    try:
        separator = sys.argv.index("--")
        source, evidence_dir, requested_source = sys.argv[separator + 1 : separator + 4]
    except (ValueError, IndexError) as exc:
        raise RuntimeError("Expected -- SOURCE_FBX EVIDENCE_DIRECTORY REQUESTED_SOURCE_FBX") from exc
    return Path(source).resolve(), Path(evidence_dir).resolve(), Path(requested_source).resolve()


SOURCE_FBX, EVIDENCE_DIR, REQUESTED_SOURCE_FBX = blender_args()


def export_current_scene(subject, path: Path) -> None:
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    subject.export_character(path, armatures[0], meshes)


def make_generated_split_fixture(subject, path: Path) -> None:
    """Create a derived test fixture, not an independently authored character."""
    subject.import_fbx(SOURCE_FBX)
    armature, meshes = subject.character_objects()
    source_mesh = meshes[0]

    for obj in (armature, source_mesh):
        obj.scale.z = 1.12
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        obj.select_set(False)

    split_objects = []
    for name, keep_positive in (("GeneratedBody_A", False), ("GeneratedBody_B", True)):
        duplicate = source_mesh.copy()
        duplicate.data = source_mesh.data.copy()
        duplicate.name = name
        duplicate.data.name = name + "Mesh"
        bpy.context.collection.objects.link(duplicate)
        mesh = bmesh.new()
        mesh.from_mesh(duplicate.data)
        remove = [vertex for vertex in mesh.verts if (vertex.co.x >= 0.0) != keep_positive]
        bmesh.ops.delete(mesh, geom=remove, context="VERTS")
        mesh.to_mesh(duplicate.data)
        mesh.free()
        duplicate.data.update()
        split_objects.append(duplicate)

    bpy.data.objects.remove(source_mesh, do_unlink=True)
    subject.export_character(path, armature, split_objects)


def make_generated_variable_axial_fixture(subject, path: Path) -> None:
    """Add an unweighted conventional axial helper between chest and neck."""
    subject.import_fbx(SOURCE_FBX)
    armature, meshes = subject.character_objects()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = armature.data.edit_bones
    parent = edit_bones["DEF-spine.003"]
    child = edit_bones["DEF-spine.004"]
    helper = edit_bones.new("DEF-spine.007")
    helper.head = parent.tail
    helper.tail = child.head if (child.head - parent.tail).length > 1e-5 else parent.tail.copy()
    if (helper.tail - helper.head).length <= 1e-5:
        helper.tail.z += 0.05
    helper.parent = parent
    helper.use_connect = False
    child.parent = helper
    child.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    subject.export_character(path, armature, meshes)


def make_generated_unweighted_optional_def_fixture(subject, path: Path) -> None:
    """Add unweighted DEF finger/face chains that must survive pruning."""
    subject.import_fbx(SOURCE_FBX)
    armature, meshes = subject.character_objects()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = armature.data.edit_bones
    finger = edit_bones.new("DEF-finger_optional.L")
    finger.head = edit_bones["DEF-hand.L"].tail
    finger.tail = finger.head.copy()
    finger.tail.x += 0.05
    finger.parent = edit_bones["DEF-hand.L"]
    face = edit_bones.new("DEF-face_optional")
    face.head = edit_bones["DEF-spine.006"].tail
    face.tail = face.head.copy()
    face.tail.z += 0.05
    face.parent = edit_bones["DEF-spine.006"]
    bpy.ops.object.mode_set(mode="OBJECT")
    subject.export_character(path, armature, meshes)


class PrepareRigifyHumanoidTests(unittest.TestCase):
    def test_roundtrip_rejects_renamed_or_reparented_unweighted_deform_bones(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        snapshot = self.subject.mc.snapshot(armature, meshes[0])
        weighted = self.subject.mc.weighted_bone_names(meshes[0])
        optional = next(name for name in snapshot["bone_names"] if name.startswith("DEF-") and name not in weighted)

        renamed = copy.deepcopy(snapshot)
        replacement = optional + "_renamed"
        renamed["bone_names"] = [replacement if name == optional else name for name in renamed["bone_names"]]
        renamed["parents"][replacement] = renamed["parents"].pop(optional)
        renamed["rest_matrices"][replacement] = renamed["rest_matrices"].pop(optional)
        with self.assertRaisesRegex(RuntimeError, "bone names"):
            self.subject._enforce_roundtrip({"fixture": snapshot}, {"fixture": renamed})

        reparented = copy.deepcopy(snapshot)
        reparented["parents"][optional] = None
        self.assertNotEqual(snapshot["parents"][optional], reparented["parents"][optional])
        with self.assertRaisesRegex(RuntimeError, "bone parents"):
            self.subject._enforce_roundtrip({"fixture": snapshot}, {"fixture": reparented})

    @classmethod
    def setUpClass(cls):
        cls.subject = load_subject()
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_real_character_writes_exact_validated_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result"
            result = self.subject.prepare(SOURCE_FBX, output)

            self.assertEqual(
                set(result),
                {
                    "schemaVersion", "status", "sourceSha256", "outputSha256", "profile",
                    "boneCountBefore", "boneCountAfter", "weightedBoneCount",
                    "meshCount", "humanBones", "removedBones", "meshes",
                },
            )
            self.assertEqual(result["schemaVersion"], 1)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["profile"], "rigify-def-standard-7spine-v1")
            self.assertEqual(result["meshCount"], 1)
            self.assertGreaterEqual(result["weightedBoneCount"], 1)
            self.assertEqual(len(result["sourceSha256"]), 64)
            self.assertEqual(len(result["outputSha256"]), 64)
            self.assertEqual(result["outputSha256"], self.subject.mc.sha256(output / "Humanoid.fbx"))
            self.assertTrue((output / "Humanoid.fbx").is_file())
            self.assertEqual(
                json.loads((output / "report.json").read_text(encoding="utf-8")),
                result,
            )
            mapping = {item["humanName"]: item["boneName"] for item in result["humanBones"]}
            self.assertEqual(mapping["Hips"], "DEF-spine")
            self.assertEqual(mapping["Head"], "DEF-spine.006")
            self.assertEqual(mapping["LeftUpperLeg"], "DEF-thigh.L")
            self.assertEqual(mapping["RightHand"], "DEF-hand.R")
            self.subject.import_fbx(output / "Humanoid.fbx")
            roundtrip_armature, _roundtrip_meshes = self.subject.character_objects()
            self.assertIn("DEF-breast.L", roundtrip_armature.data.bones)
            self.assertEqual(
                roundtrip_armature.data.bones["DEF-breast.L"].parent.name,
                "DEF-spine.003",
            )
            for mesh in result["meshes"]:
                self.assertEqual(
                    set(mesh),
                    {"name", "vertexCount", "maxVertexPositionError", "maxWeightError", "maxUvError"},
                )
                self.assertLessEqual(mesh["maxVertexPositionError"], 1e-5)
                self.assertLessEqual(mesh["maxWeightError"], 1e-6)
                self.assertLessEqual(mesh["maxUvError"], 1e-6)

    def test_generated_altered_proportions_split_mesh_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "generated-altered-proportions-split-mesh.fbx"
            make_generated_split_fixture(self.subject, fixture)
            output = root / "converted"

            result = self.subject.prepare(fixture, output)

            self.assertEqual(result["meshCount"], 2)
            self.assertEqual(len({mesh["name"] for mesh in result["meshes"]}), 2)
            self.assertTrue(all(mesh["vertexCount"] > 0 for mesh in result["meshes"]))
            mapped = {item["boneName"] for item in result["humanBones"]}
            self.assertGreaterEqual(result["boneCountAfter"], len(mapped))
            self.assertTrue(mapped.isdisjoint(result["removedBones"]))

    def test_evidence_contains_four_valid_inputs_and_converted_results(self):
        fixtures = EVIDENCE_DIR / "final-fixtures"
        converted = EVIDENCE_DIR / "final-converted"
        if fixtures.exists():
            shutil.rmtree(fixtures)
        if converted.exists():
            shutil.rmtree(converted)
        fixtures.mkdir(parents=True)
        converted.mkdir(parents=True)

        real_fixture = fixtures / "reference-source.fbx"
        generated_fixture = fixtures / "generated-altered-proportions-split-mesh.fbx"
        variable_fixture = fixtures / "generated-variable-axial-chain.fbx"
        optional_fixture = fixtures / "generated-unweighted-optional-def.fbx"
        shutil.copy2(SOURCE_FBX, real_fixture)
        make_generated_split_fixture(self.subject, generated_fixture)
        make_generated_variable_axial_fixture(self.subject, variable_fixture)
        make_generated_unweighted_optional_def_fixture(self.subject, optional_fixture)

        real = self.subject.prepare(real_fixture, converted / "reference-source")
        generated = self.subject.prepare(generated_fixture, converted / "generated-split-mesh")
        variable = self.subject.prepare(variable_fixture, converted / "generated-variable-axial")
        optional = self.subject.prepare(optional_fixture, converted / "generated-unweighted-optional-def")

        self.assertEqual(real["status"], "ok")
        self.assertEqual(generated["status"], "ok")
        self.assertEqual(generated["meshCount"], 2)
        self.assertEqual(variable["profile"], "rigify-def-standard-8spine-v1")
        self.subject.import_fbx(converted / "generated-variable-axial" / "Humanoid.fbx")
        variable_armature, _variable_meshes = self.subject.character_objects()
        self.assertIn("DEF-spine.007", variable_armature.data.bones)
        self.subject.import_fbx(converted / "generated-unweighted-optional-def" / "Humanoid.fbx")
        optional_armature, _optional_meshes = self.subject.character_objects()
        self.assertIn("DEF-finger_optional.L", optional_armature.data.bones)
        self.assertIn("DEF-face_optional", optional_armature.data.bones)
        self.assertNotIn("DEF-finger_optional.L", optional["removedBones"])
        self.assertNotIn("DEF-face_optional", optional["removedBones"])
        (EVIDENCE_DIR / "fixture-provenance.json").write_text(
            json.dumps(
                {
                    "reference-source.fbx": "copied from the supplied seven-spine reference source",
                    "generated-altered-proportions-split-mesh.fbx": (
                        "generated from the supplied reference source with Z proportions changed and its mesh split; "
                        "not an independently authored character"
                    ),
                    "generated-variable-axial-chain.fbx": (
                        "generated from the supplied reference source by inserting one conventional unweighted DEF axial helper; "
                        "not an independently authored character"
                    ),
                    "generated-unweighted-optional-def.fbx": (
                        "generated from the supplied reference source with unweighted optional DEF finger and face chains; "
                        "not an independently authored character"
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_second_reference_source_converts_without_modification(self):
        source_hash = self.subject.mc.sha256(REQUESTED_SOURCE_FBX)
        with tempfile.TemporaryDirectory() as temp:
            result = self.subject.prepare(REQUESTED_SOURCE_FBX, Path(temp) / "converted")
            self.assertEqual(self.subject.mc.sha256(REQUESTED_SOURCE_FBX), source_hash)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["profile"], "rigify-def-standard-7spine-v1")

    def test_rejects_scene_content_or_skinning_modes_that_export_would_drop(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        meshes[0].modifiers.new("UnsupportedSolidify", "SOLIDIFY")
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "non-armature modifier"):
            self.subject.audit_supported_scene()

        self.subject.import_fbx(SOURCE_FBX)
        _armature, meshes = self.subject.character_objects()
        modifier = next(item for item in meshes[0].modifiers if item.type == "ARMATURE")
        modifier.use_bone_envelopes = True
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "envelope"):
            self.subject.audit_supported_scene()

        self.subject.import_fbx(SOURCE_FBX)
        _armature, meshes = self.subject.character_objects()
        extra = bpy.data.objects.new("DroppedMeshChild", None)
        bpy.context.collection.objects.link(extra)
        extra.parent = meshes[0]
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "extra scene object"):
            self.subject.audit_supported_scene()

    def test_rejects_unresolvable_weight_group_and_control_hierarchy(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        missing = meshes[0].vertex_groups.new(name="DEF-does-not-exist")
        missing.add([0], 0.25, "REPLACE")
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "without bones"):
            self.subject.audit_supported_scene()

        self.subject.import_fbx(SOURCE_FBX)
        armature, _meshes = self.subject.character_objects()
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        armature.data.edit_bones["DEF-hand.L"].parent = armature.data.edit_bones["MCH-spine"]
        bpy.ops.object.mode_set(mode="OBJECT")
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "control hierarchy"):
            self.subject._virtual_parent(armature, "DEF-hand.L")

    def test_non_finite_roundtrip_metric_is_rejected(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.audit_supported_scene()
        expected = self.subject.mc.snapshot(armature, meshes[0])
        bone_name = next(iter(expected["rest_matrices"]))
        vertex_index = next(index for index, groups in expected["weights"].items() if groups)
        group_name = next(iter(expected["weights"][vertex_index]))

        def mutate_matrix(item):
            item["rest_matrices"][bone_name][2][3] = float("inf")

        mutations = {
            "vertex-y-nan": lambda item: item["vertices"].__setitem__(
                0, (item["vertices"][0][0], float("nan"), item["vertices"][0][2])
            ),
            "vertex-z-inf": lambda item: item["vertices"].__setitem__(
                0, (item["vertices"][0][0], item["vertices"][0][1], float("inf"))
            ),
            "uv-v-nan": lambda item: item["uvs"][0].__setitem__(
                0, (item["uvs"][0][0][0], float("nan"))
            ),
            "matrix-nonfirst-inf": mutate_matrix,
            "weight-nan": lambda item: item["weights"][vertex_index].__setitem__(group_name, float("nan")),
            "weight-inf": lambda item: item["weights"][vertex_index].__setitem__(group_name, float("inf")),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                actual = copy.deepcopy(expected)
                mutate(actual)
                with self.assertRaisesRegex(RuntimeError, "non-finite"):
                    self.subject._enforce_roundtrip(
                        {meshes[0].name: expected},
                        {meshes[0].name: actual},
                    )

        invalid_expected = copy.deepcopy(expected)
        values = list(invalid_expected["vertices"][0])
        values[2] = float("nan")
        invalid_expected["vertices"][0] = tuple(values)
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            self.subject._enforce_roundtrip(
                {meshes[0].name: invalid_expected},
                {meshes[0].name: expected},
            )

    def test_optional_shoulders_and_toes_require_bilateral_pairs(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, _meshes = self.subject.character_objects()
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        armature.data.edit_bones["DEF-upper_arm.L"].parent = armature.data.edit_bones["ORG-spine.003"]
        armature.data.edit_bones["DEF-upper_arm.R"].parent = armature.data.edit_bones["ORG-spine.003"]
        for name in (
            "DEF-shoulder.L_end", "DEF-shoulder.R_end", "DEF-shoulder.L", "DEF-shoulder.R",
            "DEF-toe.L_end", "DEF-toe.R_end", "DEF-toe.L", "DEF-toe.R",
        ):
            armature.data.edit_bones.remove(armature.data.edit_bones[name])
        bpy.ops.object.mode_set(mode="OBJECT")
        _profile, mappings, _parents = self.subject.recognize_profile(armature)
        human_names = {mapping["humanName"] for mapping in mappings}
        self.assertNotIn("LeftShoulder", human_names)
        self.assertNotIn("RightShoulder", human_names)
        self.assertNotIn("LeftToes", human_names)
        self.assertNotIn("RightToes", human_names)

        bpy.context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        shoulder = armature.data.edit_bones.new("DEF-shoulder.L")
        shoulder.head = armature.data.edit_bones["DEF-spine.003"].tail
        shoulder.tail = shoulder.head.copy()
        shoulder.tail.x += 0.1
        shoulder.parent = armature.data.edit_bones["DEF-spine.003"]
        bpy.ops.object.mode_set(mode="OBJECT")
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "bilateral optional"):
            self.subject.recognize_profile(armature)

    def test_rejects_unknown_or_missing_required_layout_without_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "unknown-layout.fbx"
            self.subject.import_fbx(SOURCE_FBX)
            armature, _meshes = self.subject.character_objects()
            bpy.context.view_layer.objects.active = armature
            armature.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            armature.data.edit_bones.remove(armature.data.edit_bones["DEF-spine.006"])
            armature.data.edit_bones.remove(armature.data.edit_bones["DEF-spine.005"])
            bpy.ops.object.mode_set(mode="OBJECT")
            export_current_scene(self.subject, fixture)
            output = root / "must-not-exist"

            with self.assertRaisesRegex(self.subject.UnsupportedRigError, "required bones missing"):
                self.subject.prepare(fixture, output)
            self.assertFalse(output.exists())

    def test_rejects_ambiguous_axial_branch_and_multiple_armatures(self):
        self.subject.import_fbx(SOURCE_FBX)
        armature, _meshes = self.subject.character_objects()
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        branch = armature.data.edit_bones.new("DEF-spine.007")
        branch.head = armature.data.edit_bones["DEF-spine"].tail
        branch.tail = branch.head.copy()
        branch.tail.z += 0.1
        branch.parent = armature.data.edit_bones["DEF-spine"]
        bpy.ops.object.mode_set(mode="OBJECT")
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "Ambiguous axial"):
            self.subject.recognize_profile(armature)

        duplicate = armature.copy()
        duplicate.data = armature.data.copy()
        duplicate.name = "AmbiguousSecondArmature"
        bpy.context.collection.objects.link(duplicate)
        with self.assertRaisesRegex(self.subject.UnsupportedRigError, "one armature"):
            self.subject.character_objects()

    def test_rejects_weighted_control_without_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "weighted-control.fbx"
            self.subject.import_fbx(SOURCE_FBX)
            armature, meshes = self.subject.character_objects()
            control = next(b.name for b in armature.data.bones if b.name.startswith("ORG-"))
            group = meshes[0].vertex_groups.get(control) or meshes[0].vertex_groups.new(name=control)
            group.add([0], 0.25, "REPLACE")
            export_current_scene(self.subject, fixture)
            output = root / "must-not-exist"

            with self.assertRaisesRegex(self.subject.UnsupportedRigError, "weighted non-deform"):
                self.subject.prepare(fixture, output)
            self.assertFalse(output.exists())

    def test_rejects_animation_constraints_drivers_sockets_and_shape_keys(self):
        cases = []

        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        armature.animation_data_create().action = bpy.data.actions.new("UnsupportedAction")
        cases.append(("animation", armature, meshes))
        self.assertRaisesRegex(self.subject.UnsupportedRigError, "animation", self.subject.audit_supported_scene)

        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        armature.pose.bones[0].constraints.new("COPY_LOCATION")
        self.assertRaisesRegex(self.subject.UnsupportedRigError, "constraint", self.subject.audit_supported_scene)

        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        armature["driver_probe"] = 1.0
        armature.driver_add('["driver_probe"]')
        self.assertRaisesRegex(self.subject.UnsupportedRigError, "driver", self.subject.audit_supported_scene)

        self.subject.import_fbx(SOURCE_FBX)
        armature, meshes = self.subject.character_objects()
        socket = bpy.data.objects.new("UnsupportedSocket", None)
        bpy.context.collection.objects.link(socket)
        socket.parent = armature
        socket.parent_type = "BONE"
        socket.parent_bone = "DEF-hand.L"
        self.assertRaisesRegex(self.subject.UnsupportedRigError, "socket", self.subject.audit_supported_scene)

        self.subject.import_fbx(SOURCE_FBX)
        _armature, meshes = self.subject.character_objects()
        meshes[0].shape_key_add(name="Basis")
        meshes[0].shape_key_add(name="Expression")
        self.assertRaisesRegex(self.subject.UnsupportedRigError, "shape key", self.subject.audit_supported_scene)

    def test_output_safety_rejects_relative_missing_existing_and_source_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(ValueError, "absolute"):
                self.subject.validate_paths(Path("relative.fbx"), root / "new")
            with self.assertRaisesRegex(FileNotFoundError, "source"):
                self.subject.validate_paths(root / "missing.fbx", root / "new")
            with self.assertRaisesRegex(FileExistsError, "new staging"):
                self.subject.validate_paths(SOURCE_FBX, existing)
            with self.assertRaisesRegex(ValueError, "source"):
                self.subject.validate_paths(SOURCE_FBX, SOURCE_FBX)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
