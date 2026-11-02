using System;
using System.IO;
using System.Reflection;
using NUnit.Framework;
using RigifyHumanoid.Editor;
using UnityEditor.PackageManager;
namespace RigifyHumanoid.Tests {
public class PackageTests {
[Test] public void BackendIsResolvedFromInstalledPackage() {
 var method=typeof(RigifyHumanoidConversion).GetMethod("ResolveBackendScript",BindingFlags.NonPublic|BindingFlags.Static);
 Assert.That(method,Is.Not.Null);
 var path=(string)method.Invoke(null,null);
 var package=PackageInfo.FindForAssembly(typeof(RigifyHumanoidConversion).Assembly);
 Assert.That(path,Is.EqualTo(Path.GetFullPath(Path.Combine(package.resolvedPath,"Editor","Blender","prepare_rigify_humanoid.py"))));
 Assert.That(File.Exists(path),Is.True);
 Assert.That(File.Exists(Path.Combine(Path.GetDirectoryName(path),"rigify_fbx_utils.py")),Is.True);
}
[Test] public void OutputIsProjectNeutralAndGeneratedInputsAreRejected() {
 Assert.That(RigifyHumanoidImporter.OutputRoot,Is.EqualTo("Assets/RigifyGenerated"));
 Assert.Throws<ArgumentException>(()=>RigifyHumanoidImporter.ValidateSourcePath("Assets/RigifyGenerated/recursive.fbx"));
}
}}
