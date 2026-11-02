"""Convert a conventional Rigify DEF humanoid FBX into a validated Unity FBX.

The source is never modified. A result directory is published only after an
FBX re-import proves geometry, topology, UVs, materials, weights and rest pose.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import bpy


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import rigify_fbx_utils as mc  # noqa: E402 -- proven numeric/import helpers


class UnsupportedRigError(RuntimeError):
    """Raised when conversion would guess semantics or discard authored data."""


REQUIRED_LIMB_BONES = {
    "LeftUpperArm": "DEF-upper_arm.L",
    "LeftLowerArm": "DEF-forearm.L",
    "LeftHand": "DEF-hand.L",
    "RightUpperArm": "DEF-upper_arm.R",
    "RightLowerArm": "DEF-forearm.R",
    "RightHand": "DEF-hand.R",
    "LeftUpperLeg": "DEF-thigh.L",
    "LeftLowerLeg": "DEF-shin.L",
    "LeftFoot": "DEF-foot.L",
    "RightUpperLeg": "DEF-thigh.R",
    "RightLowerLeg": "DEF-shin.R",
    "RightFoot": "DEF-foot.R",
}

OPTIONAL_BONE_PAIRS = (
    (("LeftShoulder", "DEF-shoulder.L"), ("RightShoulder", "DEF-shoulder.R")),
    (("LeftToes", "DEF-toe.L"), ("RightToes", "DEF-toe.R")),
)

AXIAL_NAME = re.compile(r"^DEF-spine(?:\.\d{3})?$")
POSITION_TOLERANCE = 1e-5
REST_TOLERANCE = 1e-5
UV_TOLERANCE = 1e-6
WEIGHT_TOLERANCE = 1e-6


def validate_paths(source: Path, output_root: Path) -> tuple[Path, Path]:
    source = Path(source)
    output_root = Path(output_root)
    if not source.is_absolute() or not output_root.is_absolute():
        raise ValueError("Source and output root must be absolute paths")
    source = source.resolve()
    output_root = output_root.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source FBX does not exist: {source}")
    if source.suffix.lower() != ".fbx":
        raise ValueError(f"Source must be an FBX: {source}")
    if output_root == source:
        raise ValueError("Refusing to use the source FBX as output root")
    if output_root.exists():
        raise FileExistsError(f"Output root must be a new staging directory: {output_root}")
    return source, output_root


def import_fbx(path: Path) -> None:
    mc.import_fbx(path)


def character_objects():
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    all_meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if len(armatures) != 1:
        raise UnsupportedRigError(f"Expected one armature; got {len(armatures)}")
    armature = armatures[0]
    meshes = []
    for mesh in all_meshes:
        modifiers = [modifier for modifier in mesh.modifiers if modifier.type == "ARMATURE"]
        if len(modifiers) != 1 or modifiers[0].object != armature:
            raise UnsupportedRigError(f"Mesh is not uniquely skinned to the armature: {mesh.name}")
        meshes.append(mesh)
    if not meshes:
        raise UnsupportedRigError("Expected one or more skinned meshes; got 0")
    names = [mesh.name for mesh in meshes]
    if len(set(names)) != len(names):
        raise UnsupportedRigError("Skinned mesh names must be unique")
    return armature, sorted(meshes, key=lambda item: item.name)


def _driver_count(id_block) -> int:
    animation = getattr(id_block, "animation_data", None)
    return len(animation.drivers) if animation else 0


def _weighted_names(meshes) -> set[str]:
    return {
        name
        for mesh in meshes
        for values in mc.weight_map(mesh).values()
        for name in values
    }


def _assert_finite_values(label: str, values) -> None:
    for index, value in enumerate(values):
        if not math.isfinite(float(value)):
            raise RuntimeError(f"{label} contains non-finite value at scalar {index}")


def _assert_finite_snapshot(label: str, value) -> None:
    for vertex_index, vertex in enumerate(value["vertices"]):
        _assert_finite_values(f"{label} vertex {vertex_index}", vertex)
    for bone_name, matrix in value["rest_matrices"].items():
        _assert_finite_values(
            f"{label} rest matrix {bone_name}",
            (matrix[row][column] for row in range(4) for column in range(4)),
        )
    for layer_index, layer in enumerate(value["uvs"]):
        for loop_index, uv in enumerate(layer):
            _assert_finite_values(f"{label} UV {layer_index}:{loop_index}", uv)
    for vertex_index, groups in value["weights"].items():
        _assert_finite_values(f"{label} weights {vertex_index}", groups.values())


def audit_supported_scene():
    armature, meshes = character_objects()
    weighted = _weighted_names(meshes)
    bone_names = {bone.name for bone in armature.data.bones}
    unresolved = sorted(weighted - bone_names)
    if unresolved:
        raise UnsupportedRigError("weighted vertex groups without bones: " + ", ".join(unresolved))
    controls = sorted(name for name in weighted if not name.startswith("DEF-"))
    if controls:
        raise UnsupportedRigError("weighted non-deform bones: " + ", ".join(controls))

    for mesh in meshes:
        other_modifiers = [modifier.type for modifier in mesh.modifiers if modifier.type != "ARMATURE"]
        if other_modifiers:
            raise UnsupportedRigError(
                f"non-armature modifier data is unsupported on {mesh.name}: " + ", ".join(other_modifiers)
            )
        modifier = next(modifier for modifier in mesh.modifiers if modifier.type == "ARMATURE")
        if modifier.use_bone_envelopes or not modifier.use_vertex_groups:
            raise UnsupportedRigError(f"envelope-only skinning is unsupported: {mesh.name}")

    action_names = sorted(action.name for action in bpy.data.actions)
    animated = []
    for obj in [armature, *meshes]:
        data = obj.animation_data
        if data and (data.action or len(data.nla_tracks)):
            animated.append(obj.name)
    if action_names or animated:
        raise UnsupportedRigError("animation data is unsupported: " + ", ".join(action_names + animated))

    constraints = [
        f"{bone.name}:{constraint.type}"
        for bone in armature.pose.bones
        for constraint in bone.constraints
    ]
    constraints.extend(
        f"{obj.name}:{constraint.type}"
        for obj in [armature, *meshes]
        for constraint in obj.constraints
    )
    if constraints:
        raise UnsupportedRigError("constraint data is unsupported: " + ", ".join(constraints))

    driven = []
    blocks = [armature, armature.data]
    for mesh in meshes:
        blocks.extend([mesh, mesh.data, mesh.data.shape_keys])
    for block in blocks:
        if block is not None and _driver_count(block):
            driven.append(block.name)
    if driven:
        raise UnsupportedRigError("driver data is unsupported: " + ", ".join(driven))

    shaped = [mesh.name for mesh in meshes if mesh.data.shape_keys is not None]
    if shaped:
        raise UnsupportedRigError("shape key data is unsupported: " + ", ".join(shaped))

    allowed = {armature, *meshes}
    extras = sorted(obj.name for obj in bpy.context.scene.objects if obj not in allowed)
    if extras:
        raise UnsupportedRigError("extra scene object/socket data is unsupported: " + ", ".join(extras))
    return armature, meshes


def _virtual_parent(armature, bone_name: str) -> str | None:
    bone = armature.data.bones[bone_name]
    ancestor = bone.parent
    visited = set()
    while ancestor is not None:
        if ancestor.name in visited:
            raise UnsupportedRigError(f"Cyclic hierarchy while resolving {bone_name}")
        visited.add(ancestor.name)
        if ancestor.name == "root" or ancestor.name.startswith("DEF-"):
            if ancestor.name != bone_name:
                return ancestor.name
        if ancestor.name.startswith("ORG-"):
            counterpart = "DEF-" + ancestor.name[4:]
            if counterpart != bone_name and armature.data.bones.get(counterpart) is not None:
                return counterpart
        else:
            raise UnsupportedRigError(
                f"Unsupported control hierarchy while resolving {bone_name}: {ancestor.name}"
            )
        ancestor = ancestor.parent
    return None


def _path_to_ancestor(parents: dict[str, str | None], child: str, ancestor: str) -> list[str]:
    path = [child]
    while path[-1] != ancestor:
        parent = parents.get(path[-1])
        if parent is None or parent in path:
            raise UnsupportedRigError(f"Unsupported unresolved needed hierarchy: {child} -> {ancestor}")
        path.append(parent)
    path.reverse()
    return path


def recognize_profile(armature):
    names = {bone.name for bone in armature.data.bones}
    missing = sorted(set(REQUIRED_LIMB_BONES.values()) - names)
    if missing:
        raise UnsupportedRigError("required bones missing: " + ", ".join(missing))

    optional_mapping = {}
    for left, right in OPTIONAL_BONE_PAIRS:
        left_present = left[1] in names
        right_present = right[1] in names
        if left_present != right_present:
            raise UnsupportedRigError(
                f"bilateral optional bones must be both present or both absent: {left[1]}, {right[1]}"
            )
        if left_present:
            optional_mapping[left[0]] = left[1]
            optional_mapping[right[0]] = right[1]

    parents = {name: _virtual_parent(armature, name) for name in names if name.startswith("DEF-")}
    thigh_parent_l = parents["DEF-thigh.L"]
    thigh_parent_r = parents["DEF-thigh.R"]
    shoulder_parent_l = parents[
        "DEF-shoulder.L" if "DEF-shoulder.L" in optional_mapping.values() else "DEF-upper_arm.L"
    ]
    shoulder_parent_r = parents[
        "DEF-shoulder.R" if "DEF-shoulder.R" in optional_mapping.values() else "DEF-upper_arm.R"
    ]
    if thigh_parent_l != thigh_parent_r or shoulder_parent_l != shoulder_parent_r:
        raise UnsupportedRigError("Ambiguous bilateral torso attachment")
    hips = thigh_parent_l
    shoulder_attachment = shoulder_parent_l
    if hips is None or shoulder_attachment is None:
        raise UnsupportedRigError("Unsupported unresolved needed hierarchy for torso")

    torso = _path_to_ancestor(parents, shoulder_attachment, hips)
    if len(torso) < 3 or any(not AXIAL_NAME.match(name) for name in torso):
        raise UnsupportedRigError("Unknown torso layout; conventional DEF spine chain required")

    children = {}
    for child, parent in parents.items():
        if parent is not None and AXIAL_NAME.match(child):
            children.setdefault(parent, []).append(child)
    for index, parent in enumerate(torso[:-1]):
        candidates = sorted(children.get(parent, []))
        if candidates != [torso[index + 1]]:
            raise UnsupportedRigError("Ambiguous axial spine branch in torso")
    above = []
    current = shoulder_attachment
    while True:
        candidates = sorted(name for name in children.get(current, []) if name not in torso)
        if not candidates:
            break
        if len(candidates) != 1:
            raise UnsupportedRigError("Ambiguous axial spine branch above shoulders")
        current = candidates[0]
        above.append(current)
    if len(above) < 2:
        raise UnsupportedRigError("required bones missing: conventional neck/head axial chain")

    axial_mapping = {
        "Hips": torso[0],
        "Spine": torso[1],
        "Chest": torso[-1] if len(torso) == 3 else torso[-2],
        "Neck": above[-2],
        "Head": above[-1],
    }
    if len(torso) >= 4:
        axial_mapping["UpperChest"] = torso[-1]

    ordered_names = ["Hips", "Spine", "Chest"]
    if "UpperChest" in axial_mapping:
        ordered_names.append("UpperChest")
    ordered_names.extend(["Neck", "Head"])
    limb_order = [
        "LeftShoulder", "LeftUpperArm", "LeftLowerArm", "LeftHand",
        "RightShoulder", "RightUpperArm", "RightLowerArm", "RightHand",
        "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "LeftToes",
        "RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes",
    ]
    mapping = {**axial_mapping, **REQUIRED_LIMB_BONES, **optional_mapping}
    ordered_names.extend(name for name in limb_order if name in mapping)
    if len(set(mapping.values())) != len(mapping):
        raise UnsupportedRigError("Ambiguous Humanoid mapping uses a bone more than once")
    profile = f"rigify-def-standard-{len(torso) + len(above)}spine-v1"
    return profile, [{"humanName": name, "boneName": mapping[name]} for name in ordered_names], parents


def _repair_and_prune(armature, meshes, mapped_names: set[str], parents: dict[str, str | None]):
    weighted = _weighted_names(meshes)
    keep = (
        set(weighted)
        | set(mapped_names)
        | {bone.name for bone in armature.data.bones if bone.name.startswith("DEF-")}
        | {"root"}
    )
    pending = list(keep - {"root"})
    while pending:
        name = pending.pop()
        if armature.data.bones.get(name) is None:
            raise UnsupportedRigError(f"required bones missing: {name}")
        parent = parents.get(name)
        if parent is None:
            raise UnsupportedRigError(f"Unsupported unresolved needed hierarchy: {name}")
        if parent not in keep:
            keep.add(parent)
            if parent != "root":
                pending.append(parent)

    rest_before = {bone.name: bone.matrix_local.copy() for bone in armature.data.bones}
    skin_before = {mesh.name: mc.evaluated_world_vertices(mesh) for mesh in meshes}
    _assert_finite_values(
        "pre-repair rest matrices",
        (
            matrix[row][column]
            for matrix in rest_before.values()
            for row in range(4)
            for column in range(4)
        ),
    )
    for mesh in meshes:
        _assert_finite_snapshot(f"pre-repair mesh {mesh.name}", mc.snapshot(armature, mesh))
        _assert_finite_values(
            f"pre-repair evaluated vertices {mesh.name}",
            (component for vertex in skin_before[mesh.name] for component in vertex),
        )
    all_names = {bone.name for bone in armature.data.bones}
    removed = sorted(all_names - keep)

    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        matrices = {name: edit_bones[name].matrix.copy() for name in keep}
        for name in sorted(keep - {"root"}):
            parent = parents.get(name)
            if parent is None or edit_bones.get(parent) is None:
                raise UnsupportedRigError(f"Unsupported unresolved needed hierarchy: {name}")
            bone = edit_bones[name]
            bone.parent = edit_bones[parent]
            bone.use_connect = False
            bone.matrix = matrices[name]
        for name in removed:
            bone = edit_bones.get(name)
            if bone is not None:
                edit_bones.remove(bone)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()

    rest_after = {bone.name: bone.matrix_local.copy() for bone in armature.data.bones}
    skin_after = {mesh.name: mc.evaluated_world_vertices(mesh) for mesh in meshes}
    _assert_finite_values(
        "post-repair rest matrices",
        (
            matrix[row][column]
            for matrix in rest_after.values()
            for row in range(4)
            for column in range(4)
        ),
    )
    for mesh in meshes:
        _assert_finite_snapshot(f"post-repair mesh {mesh.name}", mc.snapshot(armature, mesh))
        _assert_finite_values(
            f"post-repair evaluated vertices {mesh.name}",
            (component for vertex in skin_after[mesh.name] for component in vertex),
        )

    rest_error = max(
        (mc._matrix_error(rest_before[name], rest_after[name]) for name in keep),
        default=0.0,
    )
    skin_error = max(
        (mc._max_vector_error(skin_before[mesh.name], skin_after[mesh.name]) for mesh in meshes),
        default=0.0,
    )
    if not math.isfinite(rest_error) or not math.isfinite(skin_error):
        raise RuntimeError("Hierarchy repair produced non-finite validation metrics")
    if rest_error > REST_TOLERANCE or skin_error > POSITION_TOLERANCE:
        raise RuntimeError(f"Hierarchy repair changed rest/skin data: rest={rest_error}, skin={skin_error}")
    return removed, weighted


def export_character(path: Path, armature, meshes) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    for mesh in meshes:
        mesh.select_set(True)
    bpy.context.view_layer.objects.active = armature
    result = bpy.ops.export_scene.fbx(
        filepath=str(Path(path).resolve()),
        use_selection=True,
        object_types={"ARMATURE", "MESH"},
        global_scale=1.0,
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_UNITS",
        axis_forward="-Z",
        axis_up="Y",
        use_space_transform=True,
        bake_space_transform=False,
        add_leaf_bones=False,
        use_armature_deform_only=False,
        armature_nodetype="NULL",
        bake_anim=False,
        path_mode="AUTO",
        embed_textures=False,
    )
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX export failed: {path}")


def _enforce_roundtrip(expected_by_name, actual_by_name):
    if set(expected_by_name) != set(actual_by_name):
        raise RuntimeError(
            f"Roundtrip mesh names changed: expected {sorted(expected_by_name)}, got {sorted(actual_by_name)}"
        )
    reports = []
    for name in sorted(expected_by_name):
        _assert_finite_snapshot(f"expected roundtrip mesh {name}", expected_by_name[name])
        _assert_finite_snapshot(f"actual roundtrip mesh {name}", actual_by_name[name])
        if expected_by_name[name]["bone_names"] != actual_by_name[name]["bone_names"]:
            raise RuntimeError(f"Roundtrip bone names changed for {name}")
        if expected_by_name[name]["parents"] != actual_by_name[name]["parents"]:
            raise RuntimeError(f"Roundtrip bone parents changed for {name}")
        metrics = mc.compare_snapshots(expected_by_name[name], actual_by_name[name])
        failures = []
        for key in ("vertex_count_delta", "polygon_count_delta", "uv_loop_count_delta", "material_slot_count_delta"):
            if metrics[key] != 0:
                failures.append(f"{key}={metrics[key]}")
        for key in ("topology_equal", "material_slots_equal"):
            if not metrics[key]:
                failures.append(f"{key}=false")
        if metrics["missing_weighted_bones"]:
            failures.append("missing_weighted_bones=" + ",".join(metrics["missing_weighted_bones"]))
        limits = {
            "max_rest_matrix_error": REST_TOLERANCE,
            "max_vertex_position_error": POSITION_TOLERANCE,
            "max_uv_error": UV_TOLERANCE,
            "max_weight_error": WEIGHT_TOLERANCE,
        }
        for key, limit in limits.items():
            if not math.isfinite(metrics[key]):
                failures.append(f"{key}=non-finite")
                continue
            if metrics[key] > limit:
                failures.append(f"{key}={metrics[key]}>{limit}")
        if failures:
            raise RuntimeError(f"Roundtrip validation failed for {name}: " + "; ".join(failures))
        reports.append(
            {
                "name": name,
                "vertexCount": expected_by_name[name]["vertex_count"],
                "maxVertexPositionError": metrics["max_vertex_position_error"],
                "maxWeightError": metrics["max_weight_error"],
                "maxUvError": metrics["max_uv_error"],
            }
        )
    return reports


def prepare(source: Path, output_root: Path):
    source, output_root = validate_paths(source, output_root)
    source_hash = mc.sha256(source)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        import_fbx(source)
        armature, meshes = audit_supported_scene()
        bone_count_before = len(armature.data.bones)
        profile, human_bones, parents = recognize_profile(armature)
        removed, weighted = _repair_and_prune(
            armature,
            meshes,
            {item["boneName"] for item in human_bones},
            parents,
        )
        expected = {mesh.name: mc.snapshot(armature, mesh) for mesh in meshes}
        bone_count_after = len(armature.data.bones)
        final_path = temporary / "Humanoid.fbx"
        export_character(final_path, armature, meshes)

        import_fbx(final_path)
        roundtrip_armature, roundtrip_meshes = audit_supported_scene()
        actual = {mesh.name: mc.snapshot(roundtrip_armature, mesh) for mesh in roundtrip_meshes}
        mesh_reports = _enforce_roundtrip(expected, actual)
        mapped = {item["boneName"] for item in human_bones}
        roundtrip_names = {bone.name for bone in roundtrip_armature.data.bones}
        missing_mapped = sorted(mapped - roundtrip_names)
        if missing_mapped:
            raise RuntimeError("Mapped required bones missing after roundtrip: " + ", ".join(missing_mapped))
        if len(roundtrip_armature.data.bones) != bone_count_after:
            raise RuntimeError("Roundtrip bone count changed")
        if mc.sha256(source) != source_hash:
            raise RuntimeError("Source FBX changed during conversion")

        result = {
            "schemaVersion": 1,
            "status": "ok",
            "sourceSha256": source_hash,
            "outputSha256": mc.sha256(final_path),
            "profile": profile,
            "boneCountBefore": bone_count_before,
            "boneCountAfter": bone_count_after,
            "weightedBoneCount": len(weighted),
            "meshCount": len(roundtrip_meshes),
            "humanBones": human_bones,
            "removedBones": removed,
            "meshes": mesh_reports,
        }
        (temporary / "report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, output_root)
        return result
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[sys.argv.index("--") + 1 :] if argv is None and "--" in sys.argv else argv)
    result = prepare(args.source, args.output_root)
    print(json.dumps({"status": result["status"], "profile": result["profile"], "report": str(args.output_root / "report.json")}))


if __name__ == "__main__":
    main()
