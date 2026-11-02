using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEngine;
using Object = UnityEngine.Object;

namespace RigifyHumanoid.Editor
{
    [Serializable, DataContract] public sealed class RigifyHumanBone
    {
        [DataMember(IsRequired = true)] public string humanName;
        [DataMember(IsRequired = true)] public string boneName;
    }

    [Serializable, DataContract] public sealed class RigifyMeshReport
    {
        [DataMember(IsRequired = true)] public string name;
        [DataMember(IsRequired = true)] public int vertexCount;
        [DataMember(IsRequired = true)] public float maxVertexPositionError;
        [DataMember(IsRequired = true)] public float maxWeightError;
        [DataMember(IsRequired = true)] public float maxUvError;
    }

    [Serializable, DataContract] public sealed class RigifyHumanoidReport
    {
        [DataMember(IsRequired = true)] public int schemaVersion;
        [DataMember(IsRequired = true)] public string status, profile, sourceSha256, outputSha256;
        [DataMember(IsRequired = true)] public int boneCountBefore, boneCountAfter, weightedBoneCount, meshCount;
        [DataMember(IsRequired = true)] public RigifyHumanBone[] humanBones;
        [DataMember(IsRequired = true)] public string[] removedBones;
        [DataMember(IsRequired = true)] public RigifyMeshReport[] meshes;
        [DataMember] public string sourceAssetPath, sourceGuid;
    }

    public static class RigifyHumanoidImporter
    {
        public const string OutputRoot = "Assets/RigifyGenerated";
        public static string ProjectRoot => Path.GetFullPath(Path.Combine(Application.dataPath, ".."));

        public static void ValidateSourcePath(string path)
        {
            if (string.IsNullOrEmpty(path) || !path.StartsWith("Assets/", StringComparison.Ordinal) ||
                path.Contains('\\') || path.Split('/').Any(p => p == ".." || p == "." || p.Length == 0) ||
                !path.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase) ||
                path.StartsWith(OutputRoot + "/", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Select an original FBX inside Assets, not a generated result.", nameof(path));
            RequireInsideAssets(Path.GetFullPath(Path.Combine(ProjectRoot, path)));
        }

        public static string ComputeSha256(string file)
        {
            using var stream = File.OpenRead(file);
            using var algorithm = SHA256.Create();
            return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", string.Empty).ToLowerInvariant();
        }

        public static string ResultFolder(string sourceAssetPath, string resultId)
        {
            ValidateSourcePath(sourceAssetPath);
            if (!Guid.TryParseExact(resultId, "N", out _)) throw new ArgumentException("Result ID must be a new GUID.", nameof(resultId));
            var name = Regex.Replace(Path.GetFileNameWithoutExtension(sourceAssetPath), @"[^\p{L}\p{N}_-]", "_");
            if (name.Length > 48) name = name.Substring(0, 48);
            return OutputRoot + "/" + name + "_" + resultId;
        }

        public static void ValidateReport(RigifyHumanoidReport report, string expectedSourceHash)
        {
            if (report == null || report.schemaVersion != 1 || report.status != "ok" || string.IsNullOrEmpty(report.profile) || report.removedBones == null)
                throw new InvalidDataException("Blender did not return a supported successful report.");
            if (!IsHash(report.sourceSha256) || !IsHash(report.outputSha256) ||
                !string.Equals(report.sourceSha256, expectedSourceHash, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Source hash/report mismatch; convert again from the current source.");
            if (report.meshCount < 1 || report.meshes == null || report.meshes.Length != report.meshCount ||
                report.boneCountAfter < 1 || report.boneCountBefore < report.boneCountAfter ||
                report.weightedBoneCount < 1 || report.weightedBoneCount > report.boneCountAfter)
                throw new InvalidDataException("Invalid mesh or bone inventory.");
            var meshNames = new HashSet<string>(StringComparer.Ordinal);
            foreach (var mesh in report.meshes)
                if (mesh == null || string.IsNullOrEmpty(mesh.name) || !meshNames.Add(mesh.name) || mesh.vertexCount < 1 ||
                    !Within(mesh.maxVertexPositionError, 1e-5f) || !Within(mesh.maxWeightError, 1e-6f) || !Within(mesh.maxUvError, 1e-6f))
                    throw new InvalidDataException("Mesh preservation report failed its tolerances.");
            if (report.humanBones == null) throw new InvalidDataException("Missing Humanoid mapping.");
            var roles = new HashSet<string>(StringComparer.Ordinal);
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var bone in report.humanBones)
                if (bone == null || string.IsNullOrEmpty(bone.boneName) || !HumanTrait.BoneName.Contains(bone.humanName) ||
                    !roles.Add(bone.humanName) || !names.Add(bone.boneName))
                    throw new InvalidDataException("Unknown or duplicate Humanoid role/bone.");
            for (var i = 0; i < HumanTrait.BoneCount; i++)
                if (HumanTrait.RequiredBone(i) && !roles.Contains(HumanTrait.BoneName[i]))
                    throw new InvalidDataException("Missing required Humanoid role: " + HumanTrait.BoneName[i]);
        }

        public static RigifyHumanoidReport ReadReport(string json)
        {
            try
            {
                // JsonUtility silently supplies zero for absent float fields. Required
                // data-contract fields distinguish a measured zero from missing evidence.
                using var stream = new MemoryStream(Encoding.UTF8.GetBytes(json));
                return (RigifyHumanoidReport)new DataContractJsonSerializer(typeof(RigifyHumanoidReport)).ReadObject(stream);
            }
            catch (Exception exception) when (exception is SerializationException || exception is System.Xml.XmlException ||
                                              exception is ArgumentException || exception is FormatException)
            {
                throw new InvalidDataException("Incomplete or malformed Blender verification report.", exception);
            }
        }

        public static HumanBone[] BuildMapping(Transform root, RigifyHumanBone[] mapping)
        {
            if (root == null || mapping == null) throw new ArgumentException("Model and mapping are required.");
            var transforms = root.GetComponentsInChildren<Transform>(true);
            var mapped = new Dictionary<int, Transform>();
            foreach (var bone in mapping)
            {
                var id = Array.IndexOf(HumanTrait.BoneName, bone.humanName);
                var matches = transforms.Where(t => t.name == bone.boneName).ToArray();
                if (id < 0 || matches.Length != 1 || mapped.ContainsKey(id) || mapped.ContainsValue(matches[0]))
                    throw new InvalidDataException("Ambiguous imported bone: " + bone.boneName);
                mapped.Add(id, matches[0]);
            }
            foreach (var pair in mapped)
            {
                var parent = HumanTrait.GetParentBone(pair.Key);
                while (parent >= 0 && !mapped.ContainsKey(parent)) parent = HumanTrait.GetParentBone(parent);
                if (parent >= 0 && !pair.Value.IsChildOf(mapped[parent]))
                    throw new InvalidDataException("Incorrect Humanoid ancestry: " + pair.Value.name);
            }
            return mapping.Select(b => new HumanBone { boneName = b.boneName, humanName = b.humanName,
                limit = new HumanLimit { useDefaultValues = true } }).ToArray();
        }

        public static string ImportResult(string sourceAssetPath, string stagingDirectory, string expectedSourceHash, string resultId)
        {
            ValidateSourcePath(sourceAssetPath);
            if (EditorApplication.isPlayingOrWillChangePlaymode) throw new InvalidOperationException("Leave Play Mode before conversion.");
            var sourceFile = Path.Combine(ProjectRoot, sourceAssetPath);
            var source = AssetDatabase.LoadAssetAtPath<GameObject>(sourceAssetPath);
            if (source == null) throw new InvalidDataException("Source FBX is not imported.");
            var reportFile = Path.Combine(stagingDirectory, "report.json");
            var modelFile = Path.Combine(stagingDirectory, "Humanoid.fbx");
            var report = ReadReport(File.ReadAllText(reportFile));
            ValidateReport(report, expectedSourceHash);
            RequireHash(sourceFile, expectedSourceHash);
            RequireHash(modelFile, report.outputSha256);
            var materials = SourceMaterials(source, report);
            var folder = ResultFolder(sourceAssetPath, resultId);
            var fullFolder = Path.GetFullPath(Path.Combine(ProjectRoot, folder));
            RequireInsideAssets(fullFolder);
            if (Directory.Exists(fullFolder) || File.Exists(fullFolder) || File.Exists(fullFolder + ".meta"))
                throw new IOException("A result already exists; create a new conversion instead of overwriting it.");
            EnsureFolder(OutputRoot);
            var owned = false;
            try
            {
                if (string.IsNullOrEmpty(AssetDatabase.CreateFolder(OutputRoot, Path.GetFileName(folder))))
                    throw new IOException("Could not create a new result folder.");
                owned = true;
                var modelPath = folder + "/Humanoid.fbx";
                File.Copy(modelFile, Path.Combine(ProjectRoot, modelPath), false);
                AssetDatabase.ImportAsset(modelPath, ImportAssetOptions.ForceSynchronousImport);
                ConfigureModel(modelPath, report.humanBones);
                var instance = Object.Instantiate(AssetDatabase.LoadAssetAtPath<GameObject>(modelPath));
                var prefabPath = folder + "/Humanoid.prefab";
                try
                {
                    instance.name = Path.GetFileNameWithoutExtension(sourceAssetPath) + "_Humanoid";
                    var animator = instance.GetComponent<Animator>();
                    if (animator == null || !animator.isHuman || !animator.avatar.isValid) throw new InvalidDataException("Invalid Human Avatar.");
                    animator.applyRootMotion = false;
                    animator.runtimeAnimatorController = null; // Deliver an asset, not the MC preview/gameplay controller.
                    foreach (var role in report.humanBones)
                        if (animator.GetBoneTransform((HumanBodyBones)Array.IndexOf(HumanTrait.BoneName, role.humanName)) == null)
                            throw new InvalidDataException("Runtime Humanoid bone is unavailable: " + role.humanName);
                    var skins = instance.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                    if (skins.Length != report.meshCount || skins.Select(s => s.name).Distinct().Count() != skins.Length)
                        throw new InvalidDataException("Imported mesh inventory changed.");
                    foreach (var skin in skins)
                    {
                        if (!materials.TryGetValue(skin.name, out var slots) || slots.Length != skin.sharedMaterials.Length)
                            throw new InvalidDataException("Cannot preserve material slots for " + skin.name);
                        skin.sharedMaterials = slots;
                        ValidateSkin(skin);
                    }
                    RequireHash(sourceFile, expectedSourceHash);
                    if (PrefabUtility.SaveAsPrefabAsset(instance, prefabPath) == null) throw new IOException("Could not save Humanoid prefab.");
                }
                finally { Object.DestroyImmediate(instance); }
                report.sourceAssetPath = sourceAssetPath;
                report.sourceGuid = AssetDatabase.AssetPathToGUID(sourceAssetPath);
                File.WriteAllText(Path.Combine(fullFolder, "ConversionReport.json"), JsonUtility.ToJson(report, true));
                AssetDatabase.ImportAsset(folder + "/ConversionReport.json", ImportAssetOptions.ForceSynchronousImport);
                return prefabPath;
            }
            catch
            {
                if (owned)
                {
                    RequireInsideAssets(fullFolder);
                    if (!AssetDatabase.DeleteAsset(folder)) Debug.LogError("Rigify rollback could not remove its result folder: " + folder);
                }
                throw;
            }
        }

        private static Dictionary<string, Material[]> SourceMaterials(GameObject source, RigifyHumanoidReport report)
        {
            var skins = source.GetComponentsInChildren<SkinnedMeshRenderer>(true);
            if (skins.Length != report.meshCount) throw new InvalidDataException("Source mesh inventory differs from the Blender report.");
            var materials = new Dictionary<string, Material[]>(StringComparer.Ordinal);
            foreach (var skin in skins)
            {
                if (materials.ContainsKey(skin.name) || !report.meshes.Any(m => m.name == skin.name))
                    throw new InvalidDataException("Source mesh names must be unique and match the report.");
                materials.Add(skin.name, skin.sharedMaterials);
            }
            return materials;
        }

        private static void ConfigureModel(string path, RigifyHumanBone[] mapping)
        {
            var importer = (ModelImporter)AssetImporter.GetAtPath(path);
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            var description = importer.humanDescription;
            description.human = BuildMapping(model.transform, mapping);
            description.skeleton = model.GetComponentsInChildren<Transform>(true).Select(t => new SkeletonBone
                { name = t.name, position = t.localPosition, rotation = t.localRotation, scale = t.localScale }).ToArray();
            description.upperArmTwist = description.lowerArmTwist = description.upperLegTwist = description.lowerLegTwist = .5f;
            description.armStretch = description.legStretch = .05f;
            importer.animationType = ModelImporterAnimationType.Human;
            importer.avatarSetup = ModelImporterAvatarSetup.CreateFromThisModel;
            importer.humanDescription = description;
            importer.optimizeGameObjects = importer.optimizeBones = false;
            importer.importAnimation = importer.importCameras = importer.importLights = false;
            importer.SaveAndReimport();
            var avatar = AssetDatabase.LoadAllAssetsAtPath(path).OfType<Avatar>().SingleOrDefault();
            if (avatar == null || !avatar.isValid || !avatar.isHuman) throw new InvalidDataException("Unity rejected the Humanoid mapping.");
            foreach (var bone in mapping)
                if (!avatar.humanDescription.human.Any(b => b.humanName == bone.humanName && b.boneName == bone.boneName))
                    throw new InvalidDataException("Unity changed the requested Humanoid mapping.");
        }

        private static void ValidateSkin(SkinnedMeshRenderer renderer)
        {
            if (renderer.sharedMesh == null || renderer.bones.Length == 0 || renderer.bones.Any(b => b == null))
                throw new InvalidDataException("Missing mesh or skinning bones: " + renderer.name);
            var mesh = new Mesh();
            try
            {
                renderer.BakeMesh(mesh);
                if (mesh.vertexCount == 0 || mesh.vertices.Any(p => !Finite(p.x) || !Finite(p.y) || !Finite(p.z)))
                    throw new InvalidDataException("Invalid skinned geometry: " + renderer.name);
            }
            finally { Object.DestroyImmediate(mesh); }
        }

        private static void EnsureFolder(string path)
        {
            RequireInsideAssets(Path.Combine(ProjectRoot, path));
            var parts = path.Split('/');
            var current = parts[0];
            foreach (var part in parts.Skip(1))
            {
                var next = current + "/" + part;
                if (!AssetDatabase.IsValidFolder(next) && string.IsNullOrEmpty(AssetDatabase.CreateFolder(current, part)))
                    throw new IOException("Could not create " + next);
                current = next;
            }
        }

        private static void RequireInsideAssets(string path)
        {
            var root = Path.GetFullPath(Application.dataPath);
            var full = Path.GetFullPath(path);
            if (!full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Path must remain inside this project's Assets.");
            for (var directory = new DirectoryInfo(Path.GetDirectoryName(full)); directory != null && directory.FullName.Length >= root.Length; directory = directory.Parent)
                if (directory.Exists && (directory.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new ArgumentException("Conversion paths must not traverse junctions or symbolic links.");
            if ((File.Exists(full) || Directory.Exists(full)) && (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
                throw new ArgumentException("Conversion paths must not be symbolic links.");
        }

        private static bool Finite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
        private static bool Within(float value, float tolerance) => Finite(value) && value >= 0 && value <= tolerance;
        private static bool IsHash(string value) => value != null && Regex.IsMatch(value, "\\A[0-9a-fA-F]{64}\\z");
        private static void RequireHash(string path, string hash)
        {
            if (!string.Equals(ComputeSha256(path), hash, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("File changed during conversion: " + path);
        }
    }
}
