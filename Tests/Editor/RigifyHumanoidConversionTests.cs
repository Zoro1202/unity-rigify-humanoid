using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using RigifyHumanoid.Editor;
using NUnit.Framework;

namespace RigifyHumanoid.Tests
{
    public sealed class RigifyHumanoidConversionTests
    {
        private static Type Conversion
        {
            get
            {
                var type = typeof(RigifyHumanoidConversion).Assembly.GetType("RigifyHumanoid.Editor.RigifyHumanoidConversion");
                Assert.That(type, Is.Not.Null, "Rigify conversion process runner is missing.");
                return type;
            }
        }

        [Test]
        public void PublicApiOwnsOneCancelableConversionJob()
        {
            Assert.That(Conversion.IsPublic, Is.True);
            Assert.That(typeof(IDisposable).IsAssignableFrom(Conversion), Is.True);
            AssertPublicStaticMethod("Start", typeof(void), typeof(string), typeof(string));
            AssertPublicStaticMethod("Cancel", typeof(void));
            AssertPublicStaticProperty("IsRunning", typeof(bool));
            AssertPublicStaticProperty("Status", typeof(string));
            AssertPublicStaticProperty("Log", typeof(string));
            AssertPublicStaticProperty("PrefabPath", typeof(string));
        }

        [Test]
        public void BlenderArgumentsQuotePathsWithoutUsingAShell()
        {
            var arguments = InvokePrivateStatic<string>(
                "BuildArguments",
                @"D:\Example Project\Tools\Characters\prepare_rigify_humanoid.py",
                @"D:\Example Project\Assets\Characters\Hero ""A"".fbx",
                @"D:\Example Project\Library\RigifyHumanoid\abc123");

            Assert.That(arguments, Does.StartWith("--background --factory-startup --disable-autoexec --python-exit-code 1 --python "));
            Assert.That(arguments, Does.Contain("\"D:\\Example Project\\Tools\\Characters\\prepare_rigify_humanoid.py\""));
            Assert.That(arguments, Does.Contain(" -- --source \"D:\\Example Project\\Assets\\Characters\\Hero \\\"A\\\".fbx\""));
            Assert.That(arguments, Does.EndWith(" --output-root \"D:\\Example Project\\Library\\RigifyHumanoid\\abc123\""));
        }

        [Test]
        public void QuoteArgumentPreservesTrailingBackslashesBeforeClosingQuote()
        {
            var quoted = InvokePrivateStatic<string>("QuoteArgument", @"D:\Folder With Space\");
            Assert.That(quoted, Is.EqualTo("\"D:\\Folder With Space\\\\\""));
        }

        [Test]
        public void LogRetentionIsBoundedAndKeepsNewestOutput()
        {
            var cap = (int)Conversion.GetField("MaxLogCharacters", BindingFlags.NonPublic | BindingFlags.Static)
                .GetRawConstantValue();
            Assert.That(cap, Is.InRange(4096, 1024 * 1024));

            var oldText = new string('o', cap);
            var newest = "\nNEWEST_PROCESS_OUTPUT";
            var retained = InvokePrivateStatic<string>("AppendBounded", oldText, newest);

            Assert.That(retained.Length, Is.LessThanOrEqualTo(cap));
            Assert.That(retained, Does.EndWith(newest));
            Assert.That(retained.Length, Is.EqualTo(cap));
        }

        [TestCase(true, false, 0, "Cancelled.")]
        [TestCase(false, true, 0, "Timed out after 300 seconds.")]
        [TestCase(false, false, 7, "Blender failed with exit code 7.")]
        public void TerminationStatePrioritizesCancelThenTimeoutThenExitCode(
            bool cancelRequested,
            bool timedOut,
            int exitCode,
            string expected)
        {
            Assert.That(
                InvokePrivateStatic<string>("DescribeTermination", cancelRequested, timedOut, exitCode),
                Is.EqualTo(expected));
        }

        [Test]
        public void ExitedProcessWaitsForAsyncEofButNotForever()
        {
            var grace = (double)Conversion.GetField("DrainGraceSeconds", BindingFlags.NonPublic | BindingFlags.Static)
                .GetRawConstantValue();
            Assert.That(grace, Is.InRange(0.25d, 10d));
            Assert.That(InvokePrivateStatic<bool>("ShouldFinalizeProcess", true, true, true, 10d, 10d), Is.True);
            Assert.That(InvokePrivateStatic<bool>("ShouldFinalizeProcess", true, false, false, 10d, 10d + grace / 2d), Is.False);
            Assert.That(InvokePrivateStatic<bool>("ShouldFinalizeProcess", true, false, false, 10d, 10d + grace), Is.True);
            Assert.That(InvokePrivateStatic<bool>("ShouldFinalizeProcess", false, true, true, 10d, 100d), Is.False);
        }

        [Test]
        public void DefaultBlenderPathUsesKnownSteamInstallWhenPreferenceIsEmpty()
        {
            var root = Path.Combine(Path.GetTempPath(), "RigifyBlenderPath_" + Guid.NewGuid().ToString("N"));
            var blender = Path.Combine(root, "Steam", "steamapps", "common", "Blender", "blender.exe");
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(blender));
                File.WriteAllText(blender, string.Empty);
                var window = typeof(RigifyHumanoidConversion).Assembly.GetType("RigifyHumanoid.Editor.RigifyHumanoidWindow");
                Assert.That(window, Is.Not.Null);
                var method = window.GetMethod("ResolveDefaultBlenderPath", BindingFlags.NonPublic | BindingFlags.Static);
                Assert.That(method, Is.Not.Null);
                Assert.That(method.Invoke(null, new object[] { string.Empty, root }), Is.EqualTo(blender));
                Assert.That(method.Invoke(null, new object[] { @"D:\Custom\blender.exe", root }),
                    Is.EqualTo(@"D:\Custom\blender.exe"));
            }
            finally
            {
                if (Directory.Exists(root)) Directory.Delete(root, true);
            }
        }

        [Test]
        public void ClosingWindowsJobTerminatesTheOwnedProcess()
        {
            var jobType = typeof(RigifyHumanoidConversion).Assembly.GetType("RigifyHumanoid.Editor.WindowsKillOnCloseJob");
            Assert.That(jobType, Is.Not.Null, "Windows kill-on-close process ownership is missing.");
            var assign = jobType.GetMethod("Assign", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            Assert.That(assign, Is.Not.Null);
            using var parent = Process.Start(new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -NonInteractive -Command \"[Console]::In.ReadLine() | Out-Null; " +
                            "$child = Start-Process ping.exe -ArgumentList '127.0.0.1','-n','30' -PassThru; " +
                            "[Console]::Out.WriteLine($child.Id); [Console]::Out.Flush(); Wait-Process -Id $child.Id\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true
            });
            Assert.That(parent, Is.Not.Null);
            Process child = null;
            try
            {
                var job = (IDisposable)Activator.CreateInstance(jobType, true);
                assign.Invoke(job, new object[] { parent });
                parent.StandardInput.WriteLine("assigned");
                parent.StandardInput.Flush();
                var childId = int.Parse(parent.StandardOutput.ReadLine());
                child = Process.GetProcessById(childId);
                job.Dispose();
                Assert.That(parent.WaitForExit(5000), Is.True, "Closing the job handle must terminate its parent process.");
                Assert.That(child.WaitForExit(5000), Is.True, "Closing the job handle must terminate descendants created after assignment.");
            }
            finally
            {
                if (!parent.HasExited) parent.Kill();
                if (child != null && !child.HasExited) child.Kill();
                child?.Dispose();
            }
        }

        [Test]
        public void StagingRootValidationRejectsJunctionAncestors()
        {
            var container = Path.Combine(Path.GetTempPath(), "RigifyStagingPath_" + Guid.NewGuid().ToString("N"));
            var realRoot = Path.Combine(container, "real");
            var junctionRoot = Path.Combine(container, "junction");
            Directory.CreateDirectory(realRoot);
            try
            {
                using (var junction = Process.Start(new ProcessStartInfo
                {
                    FileName = Environment.GetEnvironmentVariable("ComSpec"),
                    Arguments = "/d /c mklink /J \"" + junctionRoot + "\" \"" + realRoot + "\"",
                    UseShellExecute = false,
                    CreateNoWindow = true
                }))
                {
                    Assert.That(junction, Is.Not.Null);
                    Assert.That(junction.WaitForExit(5000), Is.True);
                    Assert.That(junction.ExitCode, Is.EqualTo(0), "Test setup could not create a directory junction.");
                }

                Assert.That(InvokePrivateStatic<bool>("IsSafeStagingRoot", realRoot), Is.True);
                Assert.That(InvokePrivateStatic<bool>("IsSafeStagingRoot", junctionRoot), Is.False);
            }
            finally
            {
                if (Directory.Exists(junctionRoot)) Directory.Delete(junctionRoot);
                if (Directory.Exists(container)) Directory.Delete(container, true);
            }
        }

        private static void AssertPublicStaticMethod(string name, Type returnType, params Type[] parameterTypes)
        {
            var method = Conversion.GetMethod(name, BindingFlags.Public | BindingFlags.Static, null, parameterTypes, null);
            Assert.That(method, Is.Not.Null, name + " public static method is missing.");
            Assert.That(method.ReturnType, Is.EqualTo(returnType));
        }

        private static void AssertPublicStaticProperty(string name, Type propertyType)
        {
            var property = Conversion.GetProperty(name, BindingFlags.Public | BindingFlags.Static);
            Assert.That(property, Is.Not.Null, name + " public static property is missing.");
            Assert.That(property.PropertyType, Is.EqualTo(propertyType));
            Assert.That(property.GetMethod.IsStatic, Is.True);
        }

        private static T InvokePrivateStatic<T>(string name, params object[] arguments)
        {
            var method = Conversion.GetMethod(name, BindingFlags.NonPublic | BindingFlags.Static);
            Assert.That(method, Is.Not.Null, name + " helper is missing.");
            return (T)method.Invoke(null, arguments);
        }
    }
}
