using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using UnityEditor;
using Debug = UnityEngine.Debug;

namespace RigifyHumanoid.Editor
{
    public sealed class RigifyHumanoidConversion : IDisposable
    {
        private const double TimeoutSeconds = 300.0;
        private const double DrainGraceSeconds = 2.0;
        private const int MaxLogCharacters = 256 * 1024;

        private static RigifyHumanoidConversion active;
        private static string lastStatus = "Idle.";
        private static string lastLog = string.Empty;
        private static string lastPrefabPath = string.Empty;

        private readonly object logGate = new object();
        private readonly string sourceAssetPath;
        private readonly string stagingDirectory;
        private readonly string expectedSourceHash;
        private readonly string resultId;
        private readonly double startedAt;
        private Process process;
        private WindowsKillOnCloseJob processJob;
        private string log = string.Empty;
        private string status = "Starting Blender...";
        private bool running;
        private bool cancelRequested;
        private bool timedOut;
        private bool stopRequested;
        private bool processStarted;
        private double exitedAt = -1.0;
        private volatile bool standardOutputClosed;
        private volatile bool standardErrorClosed;

        static RigifyHumanoidConversion()
        {
            AssemblyReloadEvents.beforeAssemblyReload += StopForEditorShutdown;
            EditorApplication.quitting += StopForEditorShutdown;
        }

        private RigifyHumanoidConversion(
            string sourceAssetPath,
            string stagingDirectory,
            string expectedSourceHash,
            string resultId)
        {
            this.sourceAssetPath = sourceAssetPath;
            this.stagingDirectory = stagingDirectory;
            this.expectedSourceHash = expectedSourceHash;
            this.resultId = resultId;
            startedAt = EditorApplication.timeSinceStartup;
        }

        public static bool IsRunning => active != null && active.running;
        public static string Status => active != null ? active.status : lastStatus;
        public static string Log => active != null ? active.GetLog() : lastLog;
        public static string PrefabPath => lastPrefabPath;

        public static void Start(string sourceAssetPath, string blenderExecutable)
        {
            if (IsRunning) throw new InvalidOperationException("A Rigify conversion is already running.");
            if (EditorApplication.isPlayingOrWillChangePlaymode)
                throw new InvalidOperationException("Leave Play Mode before conversion.");

            RigifyHumanoidImporter.ValidateSourcePath(sourceAssetPath);
            var sourceFile = Path.GetFullPath(Path.Combine(RigifyHumanoidImporter.ProjectRoot, sourceAssetPath));
            if (!File.Exists(sourceFile)) throw new FileNotFoundException("The selected source FBX does not exist.", sourceFile);

            if (string.IsNullOrWhiteSpace(blenderExecutable))
                throw new ArgumentException("Select the installed Blender executable.", nameof(blenderExecutable));
            var blenderFile = Path.GetFullPath(blenderExecutable);
            if (!string.Equals(Path.GetExtension(blenderFile), ".exe", StringComparison.OrdinalIgnoreCase) || !File.Exists(blenderFile))
                throw new FileNotFoundException("Select an existing Blender .exe.", blenderFile);

            var scriptFile = ResolveBackendScript();
            if (!File.Exists(scriptFile)) throw new FileNotFoundException("The Rigify conversion tool is missing.", scriptFile);

            var id = Guid.NewGuid().ToString("N");
            var stagingRoot = Path.GetFullPath(Path.Combine(RigifyHumanoidImporter.ProjectRoot, "Library", "RigifyHumanoid"));
            var staging = Path.GetFullPath(Path.Combine(stagingRoot, id));
            if (!IsSafeStagingRoot(stagingRoot))
                throw new IOException("Rigify staging paths cannot traverse junctions or symbolic links.");
            if (!staging.StartsWith(stagingRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                Directory.Exists(staging) || File.Exists(staging))
                throw new IOException("Could not allocate a new Rigify staging directory.");
            Directory.CreateDirectory(stagingRoot);
            if (!IsSafeStagingRoot(stagingRoot))
                throw new IOException("Rigify staging became unsafe while it was being created.");

            var job = new RigifyHumanoidConversion(
                sourceAssetPath,
                staging,
                RigifyHumanoidImporter.ComputeSha256(sourceFile),
                id);
            active = job;
            lastStatus = job.status;
            lastLog = string.Empty;
            lastPrefabPath = string.Empty;
            try
            {
                job.Begin(blenderFile, scriptFile, sourceFile);
                EditorApplication.update += PollActiveJob;
            }
            catch (Exception exception)
            {
                job.AppendLog(Environment.NewLine + "Blender startup failed: " + exception);
                if (job.processStarted)
                {
                    job.cancelRequested = true;
                    job.status = "Blender startup failed; stopping owned process...";
                    job.RequestStop();
                    job.running = true;
                    EditorApplication.update -= PollActiveJob;
                    EditorApplication.update += PollActiveJob;
                }
                else
                {
                    active = null;
                    job.ReleaseResources();
                }
                throw;
            }
        }

        public static void Cancel()
        {
            if (active == null || !active.running) return;
            active.cancelRequested = true;
            active.status = "Cancelling...";
            active.RequestStop();
        }

        public void Dispose()
        {
            if (running)
            {
                cancelRequested = true;
                RequestStop();
                status = DescribeTermination(true, false, 0);
                running = false;
            }
            ReleaseResources();
            if (ReferenceEquals(active, this))
            {
                lastStatus = status;
                lastLog = GetLog();
                active = null;
                EditorApplication.update -= PollActiveJob;
            }
        }

        private static string ResolveBackendScript()
        {
            var package = UnityEditor.PackageManager.PackageInfo.FindForAssembly(
                typeof(RigifyHumanoidConversion).Assembly);
            if (package == null || string.IsNullOrEmpty(package.resolvedPath))
                throw new InvalidOperationException("Install Rigify Humanoid Converter through Package Manager.");
            return Path.GetFullPath(Path.Combine(package.resolvedPath,
                "Editor", "Blender", "prepare_rigify_humanoid.py"));
        }

        private void Begin(string blenderFile, string scriptFile, string sourceFile)
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = blenderFile,
                Arguments = BuildArguments(scriptFile, sourceFile, stagingDirectory),
                WorkingDirectory = RigifyHumanoidImporter.ProjectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            process = new Process { StartInfo = startInfo };
            process.OutputDataReceived += OnOutputDataReceived;
            process.ErrorDataReceived += OnErrorDataReceived;
            if (!process.Start()) throw new InvalidOperationException("Blender did not start.");
            processStarted = true;
            processJob = new WindowsKillOnCloseJob();
            processJob.Assign(process);
            running = true;
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            status = "Converting in Blender...";
        }

        private static void PollActiveJob()
        {
            var job = active;
            if (job == null)
            {
                EditorApplication.update -= PollActiveJob;
                return;
            }
            job.Poll();
        }

        private void Poll()
        {
            if (!running || process == null) return;
            if (!stopRequested && EditorApplication.timeSinceStartup - startedAt >= TimeoutSeconds)
            {
                timedOut = true;
                status = "Stopping after timeout...";
                RequestStop();
            }

            bool exited;
            try { exited = process.HasExited; }
            catch (InvalidOperationException) { exited = true; }
            if (exited && exitedAt < 0.0) exitedAt = EditorApplication.timeSinceStartup;
            if (!ShouldFinalizeProcess(
                    exited,
                    standardOutputClosed,
                    standardErrorClosed,
                    exitedAt,
                    EditorApplication.timeSinceStartup))
                return;

            var exitCode = -1;
            try
            {
                var outputDrained = standardOutputClosed && standardErrorClosed;
                if (outputDrained) process.WaitForExit();
                exitCode = process.ExitCode;
                if (!outputDrained)
                {
                    status = "Failed: Blender output streams did not drain after process exit.";
                    AppendLog(Environment.NewLine + status);
                }
                else if (cancelRequested || timedOut || exitCode != 0)
                {
                    status = DescribeTermination(cancelRequested, timedOut, exitCode);
                    if (exitCode != 0 && !cancelRequested && !timedOut) Debug.LogError(status + Environment.NewLine + GetLog());
                }
                else
                {
                    status = "Importing verified Humanoid assets...";
                    lastPrefabPath = RigifyHumanoidImporter.ImportResult(
                        sourceAssetPath,
                        stagingDirectory,
                        expectedSourceHash,
                        resultId);
                    status = "Completed: " + lastPrefabPath;
                    Debug.Log("RIGIFY_HUMANOID_READY prefab=" + lastPrefabPath);
                }
            }
            catch (Exception exception)
            {
                status = "Failed: " + exception.Message;
                AppendLog(Environment.NewLine + exception);
                Debug.LogException(exception);
            }
            finally
            {
                Complete();
            }
        }

        private void Complete()
        {
            running = false;
            ReleaseResources();
            lastStatus = status;
            lastLog = GetLog();
            if (ReferenceEquals(active, this)) active = null;
            EditorApplication.update -= PollActiveJob;
        }

        private void RequestStop()
        {
            if (stopRequested) return;
            stopRequested = true;
            try
            {
                processJob?.Dispose();
                processJob = null;
                if (process != null && !process.HasExited) process.Kill();
            }
            catch (Exception exception)
            {
                AppendLog(Environment.NewLine + "Could not stop Blender: " + exception.Message);
            }
        }

        private static void StopForEditorShutdown()
        {
            var job = active;
            if (job == null) return;
            job.cancelRequested = true;
            job.RequestStop();
            try
            {
                if (job.process != null && !job.process.HasExited && !job.process.WaitForExit(10000))
                {
                    job.process.Kill();
                    job.process.WaitForExit(5000);
                }
            }
            catch (Exception exception)
            {
                job.AppendLog(Environment.NewLine + "Blender shutdown wait failed: " + exception.Message);
            }
            job.status = DescribeTermination(true, false, 0);
            if (job.process == null || job.process.HasExited)
            {
                job.running = false;
                job.Dispose();
            }
            else
            {
                job.status = "Blender termination is still pending; ownership was retained.";
                job.running = true;
            }
        }

        private void ReleaseResources()
        {
            if (process != null)
            {
                try
                {
                    if (processStarted && !process.HasExited)
                    {
                        AppendLog(Environment.NewLine + "Cleanup deferred because the owned Blender process is still running.");
                        return;
                    }
                }
                catch (InvalidOperationException)
                {
                    if (processStarted)
                    {
                        AppendLog(Environment.NewLine + "Cleanup deferred because Blender exit could not be confirmed.");
                        return;
                    }
                }
                process.OutputDataReceived -= OnOutputDataReceived;
                process.ErrorDataReceived -= OnErrorDataReceived;
                process.Dispose();
                process = null;
            }
            processJob?.Dispose();
            processJob = null;
            DeleteOwnedStagingDirectory();
        }

        private void DeleteOwnedStagingDirectory()
        {
            var root = Path.GetFullPath(Path.Combine(RigifyHumanoidImporter.ProjectRoot, "Library", "RigifyHumanoid"));
            var owned = Path.GetFullPath(stagingDirectory);
            if (!IsOwnedStagingDirectory(owned, root, resultId))
            {
                AppendLog(Environment.NewLine + "Refused to remove an unsafe staging path: " + owned);
                return;
            }
            try
            {
                if (Directory.Exists(owned)) Directory.Delete(owned, true);
            }
            catch (Exception exception)
            {
                AppendLog(Environment.NewLine + "Could not remove staging directory: " + exception.Message);
            }
        }

        private void OnOutputDataReceived(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) standardOutputClosed = true;
            else AppendLog(args.Data + Environment.NewLine);
        }

        private void OnErrorDataReceived(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) standardErrorClosed = true;
            else AppendLog(args.Data + Environment.NewLine);
        }

        private void AppendLog(string value)
        {
            lock (logGate) log = AppendBounded(log, value);
        }

        private string GetLog()
        {
            lock (logGate) return log;
        }

        private static string BuildArguments(string scriptFile, string sourceFile, string stagingDirectory) =>
            "--background --factory-startup --disable-autoexec --python-exit-code 1 --python " + QuoteArgument(scriptFile) +
            " -- --source " + QuoteArgument(sourceFile) + " --output-root " + QuoteArgument(stagingDirectory);

        private static string QuoteArgument(string value)
        {
            if (value == null) throw new ArgumentNullException(nameof(value));
            var result = new StringBuilder(value.Length + 2).Append('"');
            var backslashes = 0;
            foreach (var character in value)
            {
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (character == '"')
                {
                    result.Append('\\', backslashes * 2 + 1).Append('"');
                    backslashes = 0;
                    continue;
                }
                result.Append('\\', backslashes).Append(character);
                backslashes = 0;
            }
            return result.Append('\\', backslashes * 2).Append('"').ToString();
        }

        private static string AppendBounded(string current, string addition)
        {
            var combined = (current ?? string.Empty) + (addition ?? string.Empty);
            return combined.Length <= MaxLogCharacters
                ? combined
                : combined.Substring(combined.Length - MaxLogCharacters, MaxLogCharacters);
        }

        private static bool ShouldFinalizeProcess(
            bool exited,
            bool outputClosed,
            bool errorClosed,
            double processExitedAt,
            double now) =>
            exited && ((outputClosed && errorClosed) ||
                       processExitedAt >= 0.0 && now - processExitedAt >= DrainGraceSeconds);

        private static bool IsOwnedStagingDirectory(string owned, string root, string id)
        {
            if (!string.Equals(Path.GetDirectoryName(owned), root, StringComparison.OrdinalIgnoreCase) ||
                !string.Equals(Path.GetFileName(owned), id, StringComparison.Ordinal) ||
                !Guid.TryParseExact(id, "N", out _))
                return false;
            for (var directory = new DirectoryInfo(owned); directory != null; directory = directory.Parent)
            {
                if (directory.Exists && (directory.Attributes & FileAttributes.ReparsePoint) != 0) return false;
                if (string.Equals(directory.FullName, root, StringComparison.OrdinalIgnoreCase)) return true;
                if (directory.FullName.Length < root.Length) return false;
            }
            return false;
        }

        private static bool IsSafeStagingRoot(string path)
        {
            var full = Path.GetFullPath(path);
            for (var directory = new DirectoryInfo(full); directory != null; directory = directory.Parent)
                if (directory.Exists && (directory.Attributes & FileAttributes.ReparsePoint) != 0) return false;
            return true;
        }

        private static string DescribeTermination(bool wasCancelled, bool wasTimedOut, int exitCode)
        {
            if (wasCancelled) return "Cancelled.";
            if (wasTimedOut) return "Timed out after 300 seconds.";
            return "Blender failed with exit code " + exitCode + ".";
        }
    }
}
