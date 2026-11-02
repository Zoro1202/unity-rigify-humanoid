"""Prepare the supplied MC Rigify FBX as a derived Unity Humanoid skeleton.

This script must run inside Blender.  It repairs only bone parenting while
preserving armature-space rest matrices, writes a repaired checkpoint, then
removes only dependency-free unweighted bones and validates an FBX round trip.
It never modifies the source FBX.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import bpy


REPAIR_PARENTS = {
    "DEF-pelvis.L": "DEF-spine",
    "DEF-pelvis.R": "DEF-spine",
    "DEF-thigh.L": "DEF-spine",
    "DEF-thigh.R": "DEF-spine",
    "DEF-shoulder.L": "DEF-spine.003",
    "DEF-shoulder.R": "DEF-spine.003",
    "DEF-upper_arm.L": "DEF-shoulder.L",
    "DEF-upper_arm.R": "DEF-shoulder.R",
    "DEF-breast.L": "DEF-spine.003",
    "DEF-breast.R": "DEF-spine.003",
}


class UnsupportedRigError(RuntimeError):
    """Raised when safe conversion would discard authored rig behavior."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_output_paths(
    source,
    repaired,
    final,
    report,
    allowed_output_root,
    force=False,
    stage="all",
):
    source = Path(source).resolve()
    repaired, final, report = (Path(value).resolve() for value in (repaired, final, report))
    outputs = [repaired, final, report]
    root = Path(allowed_output_root).resolve()
    if any(output == source for output in outputs):
        raise ValueError("Refusing to overwrite source FBX")
    if any(not _inside(output, root) for output in outputs):
        raise ValueError(f"Output path is outside allowed root: {root}")
    if len(set(outputs)) != len(outputs):
        raise ValueError("Output paths must be distinct")
    if stage == "repaired" and final.exists():
        raise FileExistsError(
            "Refusing repaired-stage overwrite while the final FBX exists; "
            "use --stage all to update the owned artifact set"
        )
    existing = [output for output in outputs if output.exists()]
    if existing and not force:
        raise FileExistsError("Refusing to overwrite existing outputs: " + ", ".join(map(str, existing)))
    if existing and force:
        try:
            prior = json.loads(report.read_text(encoding="utf-8"))
            same_source = Path(prior["source_path"]).resolve() == source
            owned_exports = {Path(value).resolve() for value in prior.get("exports", {}).values()}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            same_source = False
            owned_exports = set()
        existing_fbx = {output for output in (repaired, final) if output.exists()}
        if not same_source or not existing_fbx.issubset(owned_exports):
            raise FileExistsError("--force may overwrite only owned outputs for the same source")
    root.mkdir(parents=True, exist_ok=True)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_fbx(path):
    reset_scene()
    result = bpy.ops.import_scene.fbx(
        filepath=str(Path(path).resolve()),
        use_anim=True,
        ignore_leaf_bones=False,
        automatic_bone_orientation=False,
    )
    if "FINISHED" not in result:
        raise RuntimeError(f"FBX import failed: {path}")


def character_objects():
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    meshes = [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "MESH" and any(mod.type == "ARMATURE" for mod in obj.modifiers)
    ]
    if len(armatures) != 1 or len(meshes) != 1:
        raise UnsupportedRigError(
            f"Expected one armature and one skinned mesh; got {len(armatures)} and {len(meshes)}"
        )
    return armatures[0], meshes[0]


def weight_map(mesh):
    result = {}
    for vertex in mesh.data.vertices:
        values = {}
        for membership in vertex.groups:
            if membership.weight > 0.0:
                values[mesh.vertex_groups[membership.group].name] = float(membership.weight)
        result[vertex.index] = values
    return result


def weighted_bone_names(mesh):
    return {name for values in weight_map(mesh).values() for name in values}


def _driver_count(id_block):
    animation_data = getattr(id_block, "animation_data", None)
    return len(animation_data.drivers) if animation_data else 0


def audit_supported_scene():
    armature, mesh = character_objects()
    weighted = weighted_bone_names(mesh)
    non_deform = sorted(name for name in weighted if not name.startswith("DEF-"))
    if non_deform:
        raise UnsupportedRigError("weighted non-deform bones: " + ", ".join(non_deform))

    action_names = [action.name for action in bpy.data.actions]
    nla_tracks = []
    for obj in (armature, mesh):
        data = obj.animation_data
        if data and (data.action or len(data.nla_tracks)):
            nla_tracks.append(obj.name)
    if action_names or nla_tracks:
        raise UnsupportedRigError("animation data is unsupported: " + ", ".join(action_names + nla_tracks))

    constraints = [
        f"{bone.name}:{constraint.type}"
        for bone in armature.pose.bones
        for constraint in bone.constraints
    ]
    constraints.extend(f"{obj.name}:{c.type}" for obj in (armature, mesh) for c in obj.constraints)
    if constraints:
        raise UnsupportedRigError("constraint data is unsupported: " + ", ".join(constraints))

    driven = []
    for id_block in (armature, armature.data, mesh, mesh.data, mesh.data.shape_keys):
        if id_block is not None and _driver_count(id_block):
            driven.append(id_block.name)
    if driven:
        raise UnsupportedRigError("driver data is unsupported: " + ", ".join(driven))

    sockets = [
        obj.name
        for obj in bpy.context.scene.objects
        if obj not in (armature, mesh)
        and (obj.parent == armature or obj.parent_type == "BONE")
    ]
    if sockets:
        raise UnsupportedRigError("bone/socket dependencies are unsupported: " + ", ".join(sockets))
    return armature, mesh


def _matrix_error(left, right):
    return max(abs(left[row][column] - right[row][column]) for row in range(4) for column in range(4))


def evaluated_world_vertices(mesh):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = mesh.evaluated_get(depsgraph)
    temporary = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        world = evaluated.matrix_world
        return [tuple(world @ vertex.co) for vertex in temporary.vertices]
    finally:
        evaluated.to_mesh_clear()


def _max_vector_error(left, right):
    if len(left) != len(right):
        return math.inf
    return max((max(abs(a - b) for a, b in zip(x, y)) for x, y in zip(left, right)), default=0.0)


def repair_hierarchy(armature, mesh):
    rest_before = {bone.name: bone.matrix_local.copy() for bone in armature.data.bones}
    skin_before = evaluated_world_vertices(mesh)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        missing = sorted((set(REPAIR_PARENTS) | set(REPAIR_PARENTS.values())) - {b.name for b in edit_bones})
        if missing:
            raise UnsupportedRigError("required bones missing: " + ", ".join(missing))
        matrices = {name: edit_bones[name].matrix.copy() for name in REPAIR_PARENTS}
        for child_name, parent_name in REPAIR_PARENTS.items():
            child = edit_bones[child_name]
            child.parent = edit_bones[parent_name]
            child.use_connect = False
            child.matrix = matrices[child_name]
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()

    rest_error = max(
        _matrix_error(rest_before[name], armature.data.bones[name].matrix_local)
        for name in rest_before
    )
    skin_error = _max_vector_error(skin_before, evaluated_world_vertices(mesh))
    return {"max_rest_matrix_error": rest_error, "max_skin_world_position_error": skin_error}


def snapshot(armature, mesh):
    weights = weight_map(mesh)
    uv_values = []
    for layer in mesh.data.uv_layers:
        uv_values.append([tuple(item.uv) for item in layer.data])
    return {
        "bones": len(armature.data.bones),
        "bone_names": sorted(bone.name for bone in armature.data.bones),
        "weighted_bones": len({name for values in weights.values() for name in values}),
        "parents": {
            bone.name: bone.parent.name if bone.parent else None for bone in armature.data.bones
        },
        "vertex_count": len(mesh.data.vertices),
        "polygon_count": len(mesh.data.polygons),
        "uv_loop_count": sum(len(layer.data) for layer in mesh.data.uv_layers),
        "material_slots": [slot.material.name if slot.material else None for slot in mesh.material_slots],
        "topology": [tuple(polygon.vertices) for polygon in mesh.data.polygons],
        "rest_matrices": {bone.name: bone.matrix_local.copy() for bone in armature.data.bones},
        "vertices": [tuple(mesh.matrix_world @ vertex.co) for vertex in mesh.data.vertices],
        "weights": weights,
        "uvs": uv_values,
    }


def export_character(path, armature, mesh):
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
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


def prune_unweighted_bones(armature, mesh):
    weighted = weighted_bone_names(mesh)
    keep = set(weighted) | {"root"}
    for name in list(keep):
        bone = armature.data.bones.get(name)
        while bone and bone.parent:
            bone = bone.parent
            keep.add(bone.name)
    candidates = sorted(b.name for b in armature.data.bones if b.name not in keep)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for name in candidates:
            bone = armature.data.edit_bones.get(name)
            if bone is not None:
                armature.data.edit_bones.remove(bone)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    missing = sorted(weighted - {b.name for b in armature.data.bones})
    if missing:
        raise RuntimeError("Prune removed weighted bones: " + ", ".join(missing))
    return candidates


def compare_snapshots(expected, actual):
    weight_error = 0.0
    all_vertices = set(expected["weights"]) | set(actual["weights"])
    for index in all_vertices:
        before = expected["weights"].get(index, {})
        after = actual["weights"].get(index, {})
        for name in set(before) | set(after):
            weight_error = max(weight_error, abs(before.get(name, 0.0) - after.get(name, 0.0)))
    expected_uvs = [value for layer in expected["uvs"] for value in layer]
    actual_uvs = [value for layer in actual["uvs"] for value in layer]
    common_bones = set(expected["rest_matrices"]) & set(actual["rest_matrices"])
    rest_error = max(
        (_matrix_error(expected["rest_matrices"][name], actual["rest_matrices"][name]) for name in common_bones),
        default=0.0,
    )
    expected_weighted = {name for values in expected["weights"].values() for name in values}
    actual_bones = set(actual["bone_names"])
    return {
        "vertex_count_delta": actual["vertex_count"] - expected["vertex_count"],
        "polygon_count_delta": actual["polygon_count"] - expected["polygon_count"],
        "uv_loop_count_delta": actual["uv_loop_count"] - expected["uv_loop_count"],
        "material_slot_count_delta": len(actual["material_slots"]) - len(expected["material_slots"]),
        "topology_equal": expected["topology"] == actual["topology"],
        "material_slots_equal": expected["material_slots"] == actual["material_slots"],
        "missing_weighted_bones": sorted(expected_weighted - actual_bones),
        "roundtrip_imported_bones": actual["bones"],
        "max_rest_matrix_error": rest_error,
        "max_vertex_position_error": _max_vector_error(expected["vertices"], actual["vertices"]),
        "max_uv_error": _max_vector_error(expected_uvs, actual_uvs),
        "max_weight_error": weight_error,
    }


def prepare(source, repaired, final, report, allowed_output_root, force=False, stage="all"):
    if stage not in {"repaired", "all"}:
        raise ValueError(f"Unsupported stage: {stage}")
    source = Path(source).resolve()
    repaired = Path(repaired).resolve()
    final = Path(final).resolve()
    report = Path(report).resolve()
    validate_output_paths(
        source,
        repaired,
        final,
        report,
        allowed_output_root,
        force=force,
        stage=stage,
    )
    source_hash_before = sha256(source)

    import_fbx(source)
    armature, mesh = audit_supported_scene()
    source_snapshot = snapshot(armature, mesh)
    repair_metrics = repair_hierarchy(armature, mesh)
    repaired_snapshot = snapshot(armature, mesh)
    export_character(repaired, armature, mesh)

    common_result = {
        "schema_version": 1,
        "stage": stage,
        "source_path": str(source),
        "source_sha256_before": source_hash_before,
        "source": {key: source_snapshot[key] for key in ("bones", "bone_names", "weighted_bones", "parents", "vertex_count", "polygon_count", "uv_loop_count", "material_slots")},
        "repair": repair_metrics,
        "repaired": {key: repaired_snapshot[key] for key in ("bones", "bone_names", "weighted_bones", "parents", "vertex_count", "polygon_count", "uv_loop_count", "material_slots")},
        "exports": {"repaired": str(repaired)},
        "export_settings": {"axis_forward": "-Z", "axis_up": "Y", "global_scale": 1.0, "add_leaf_bones": False, "bake_anim": False},
    }
    if stage == "repaired":
        source_hash_after = sha256(source)
        if source_hash_after != source_hash_before:
            raise RuntimeError("Source FBX changed during conversion")
        common_result["source_sha256_after"] = source_hash_after
        report.write_text(json.dumps(common_result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return common_result

    removed = prune_unweighted_bones(armature, mesh)
    pruned_snapshot = snapshot(armature, mesh)
    export_character(final, armature, mesh)

    import_fbx(final)
    roundtrip_armature, roundtrip_mesh = audit_supported_scene()
    roundtrip_snapshot = snapshot(roundtrip_armature, roundtrip_mesh)
    roundtrip = compare_snapshots(pruned_snapshot, roundtrip_snapshot)
    source_hash_after = sha256(source)
    if source_hash_after != source_hash_before:
        raise RuntimeError("Source FBX changed during conversion")

    result = {
        **common_result,
        "source_sha256_after": source_hash_after,
        "prune": {"removed_bones": removed, "removed_count": len(removed)},
        "pruned": {key: pruned_snapshot[key] for key in ("bones", "bone_names", "weighted_bones", "parents", "vertex_count", "polygon_count", "uv_loop_count", "material_slots")},
        "roundtrip": roundtrip,
    }
    result["exports"]["final"] = str(final)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--stage", choices=("repaired", "all"), default="all")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def completion_summary(result):
    stage_snapshot = result["pruned"] if "pruned" in result else result["repaired"]
    return {"status": "ok", "stage": result["stage"], "bones": stage_snapshot["bones"]}


def main(argv=None):
    args = parse_args(sys.argv[sys.argv.index("--") + 1 :] if argv is None and "--" in sys.argv else argv)
    root = args.output_root.resolve()
    result = prepare(
        args.source,
        root / "MC_Humanoid_Repaired.fbx",
        root / "MC_Humanoid.fbx",
        root / "MC_Humanoid.report.json",
        root,
        force=args.force,
        stage=args.stage,
    )
    summary = completion_summary(result)
    summary["report"] = str(root / "MC_Humanoid.report.json")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
