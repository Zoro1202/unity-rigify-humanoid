using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace RigifyHumanoid.Editor
{
    public sealed class RigifyHumanoidWindow : EditorWindow
    {
        private const string BlenderPathPreference = "RigifyHumanoid.BlenderExecutable";

        private GameObject sourceFbx;
        private string blenderExecutable;
        private string localMessage = string.Empty;
        private Vector2 logScroll;

        [MenuItem("Tools/Rigify Humanoid/Converter")]
        public static void Open()
        {
            var window = GetWindow<RigifyHumanoidWindow>("Rigify Humanoid");
            window.minSize = new Vector2(540f, 420f);
            window.UseSelectionIfFbx();
            window.Show();
        }

        private void OnEnable()
        {
            blenderExecutable = ResolveDefaultBlenderPath(
                EditorPrefs.GetString(BlenderPathPreference, string.Empty),
                Environment.GetEnvironmentVariable("ProgramFiles(x86)"));
            if (sourceFbx == null) UseSelectionIfFbx();
        }

        private void OnDisable()
        {
            if (RigifyHumanoidConversion.IsRunning) RigifyHumanoidConversion.Cancel();
        }

        private void Update()
        {
            if (RigifyHumanoidConversion.IsRunning) Repaint();
        }

        private void OnGUI()
        {
            EditorGUILayout.LabelField("Reusable Rigify Humanoid Converter", EditorStyles.boldLabel);
            EditorGUILayout.HelpBox(
                "Supported: a static human FBX in Assets with a conventional Rigify deform skeleton and one or more skinned meshes. " +
                "Animation, constraints, drivers, sockets, shape keys, and customized or ambiguous skeleton layouts are not supported.",
                MessageType.Info);

            using (new EditorGUI.DisabledScope(RigifyHumanoidConversion.IsRunning))
            {
                sourceFbx = (GameObject)EditorGUILayout.ObjectField("Source FBX", sourceFbx, typeof(GameObject), false);
                DrawBlenderPath();
            }

            EditorGUILayout.Space();
            using (new EditorGUILayout.HorizontalScope())
            {
                using (new EditorGUI.DisabledScope(RigifyHumanoidConversion.IsRunning ||
                                                    EditorApplication.isPlayingOrWillChangePlaymode ||
                                                    sourceFbx == null))
                {
                    if (GUILayout.Button("Convert", GUILayout.Height(28f))) StartConversion();
                }
                using (new EditorGUI.DisabledScope(!RigifyHumanoidConversion.IsRunning))
                {
                    if (GUILayout.Button("Cancel", GUILayout.Height(28f))) RigifyHumanoidConversion.Cancel();
                }
            }

            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Status", EditorStyles.boldLabel);
            EditorGUILayout.HelpBox(CurrentStatus(), StatusMessageType());

            var prefabPath = RigifyHumanoidConversion.PrefabPath;
            using (new EditorGUI.DisabledScope(string.IsNullOrEmpty(prefabPath)))
            {
                if (GUILayout.Button("Select Output Prefab"))
                {
                    var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(prefabPath);
                    Selection.activeObject = prefab;
                    if (prefab != null) EditorGUIUtility.PingObject(prefab);
                }
            }

            EditorGUILayout.Space();
            EditorGUILayout.LabelField("Blender Log", EditorStyles.boldLabel);
            logScroll = EditorGUILayout.BeginScrollView(logScroll, GUILayout.ExpandHeight(true));
            EditorGUILayout.TextArea(RigifyHumanoidConversion.Log, GUILayout.ExpandHeight(true));
            EditorGUILayout.EndScrollView();
        }

        private void DrawBlenderPath()
        {
            using (new EditorGUILayout.HorizontalScope())
            {
                var edited = EditorGUILayout.TextField("Blender executable", blenderExecutable);
                if (!string.Equals(edited, blenderExecutable, StringComparison.Ordinal))
                {
                    blenderExecutable = edited;
                    EditorPrefs.SetString(BlenderPathPreference, blenderExecutable ?? string.Empty);
                }
                if (GUILayout.Button("Browse...", GUILayout.Width(90f)))
                {
                    var directory = string.IsNullOrEmpty(blenderExecutable)
                        ? string.Empty
                        : Path.GetDirectoryName(blenderExecutable);
                    var selected = EditorUtility.OpenFilePanel("Select Blender executable", directory, "exe");
                    if (!string.IsNullOrEmpty(selected))
                    {
                        blenderExecutable = selected;
                        EditorPrefs.SetString(BlenderPathPreference, selected);
                    }
                }
            }
        }

        private void StartConversion()
        {
            try
            {
                var sourcePath = AssetDatabase.GetAssetPath(sourceFbx);
                RigifyHumanoidConversion.Start(sourcePath, blenderExecutable);
                localMessage = string.Empty;
            }
            catch (Exception exception)
            {
                localMessage = exception.Message;
                Debug.LogException(exception);
            }
        }

        private void UseSelectionIfFbx()
        {
            var selected = Selection.activeObject as GameObject;
            var path = AssetDatabase.GetAssetPath(selected);
            if (selected != null && path.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase)) sourceFbx = selected;
        }

        private string CurrentStatus() => string.IsNullOrEmpty(localMessage)
            ? RigifyHumanoidConversion.Status
            : "Cannot start: " + localMessage;

        private MessageType StatusMessageType()
        {
            var current = CurrentStatus();
            return current.StartsWith("Failed", StringComparison.Ordinal) ||
                   current.StartsWith("Cannot", StringComparison.Ordinal) ||
                   current.StartsWith("Blender failed", StringComparison.Ordinal)
                ? MessageType.Error
                : MessageType.None;
        }

        private static string ResolveDefaultBlenderPath(string savedPath, string programFilesX86)
        {
            if (!string.IsNullOrEmpty(savedPath)) return savedPath;
            if (string.IsNullOrEmpty(programFilesX86)) return string.Empty;
            var steamBlender = Path.Combine(
                programFilesX86,
                "Steam",
                "steamapps",
                "common",
                "Blender",
                "blender.exe");
            return File.Exists(steamBlender) ? steamBlender : string.Empty;
        }
    }
}
