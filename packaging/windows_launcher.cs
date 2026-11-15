// Single-file distribution: choose a writable, private extraction directory
// before starting the existing PyInstaller one-folder application.
// Uses .NET Framework 4.x, included in supported Windows 10/11 versions.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class MoonLyricsLauncher
{
    private static string applicationDirectory;

    [STAThread]
    private static int Main(string[] arguments)
    {
        string extraction = null;
        string extractionRoot = null;
        try
        {
            applicationDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            var failures = new List<string>();
            foreach (string root in CandidateRoots())
            {
                string candidate = null;
                try
                {
                    Directory.CreateDirectory(root);
                    candidate = Path.Combine(root, "run-" + Guid.NewGuid().ToString("N"));
                    var security = new DirectorySecurity();
                    security.SetAccessRuleProtection(true, false);
                    var inheritance = InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit;
                    security.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User,
                        FileSystemRights.FullControl, inheritance, PropagationFlags.None, AccessControlType.Allow));
                    security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                        FileSystemRights.FullControl, inheritance, PropagationFlags.None, AccessControlType.Allow));
                    Directory.CreateDirectory(candidate, security);
                    string probe = Path.Combine(candidate, ".write-check");
                    using (var file = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        file.WriteByte(0);
                    File.Delete(probe);
                    extraction = candidate;
                    extractionRoot = root;
                    break;
                }
                catch (Exception error)
                {
                    if (!(error is IOException || error is UnauthorizedAccessException || error is System.Security.SecurityException))
                        throw;
                    failures.Add(root + ": " + error.Message);
                    Cleanup(root, candidate);
                }
            }
            if (extraction == null)
                throw new IOException("No writable application folder was found.\r\n" + string.Join("\r\n", failures));

            ExtractPayload(extraction);
            string executable = Path.Combine(extraction, "MoonLyrics.exe");
            if (!File.Exists(executable) || !File.Exists(Path.Combine(extraction, "_internal", "VERSION")))
                throw new InvalidDataException("The bundled application is incomplete.");

            var start = new ProcessStartInfo(executable);
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WorkingDirectory = applicationDirectory;
            start.Arguments = JoinArguments(arguments);
            // Keep settings outside the disposable payload if AppData is not writable.
            start.EnvironmentVariables["MOONLYRICS_LAUNCHER_DIRECTORY"] = applicationDirectory;
            using (var child = Process.Start(start))
            {
                if (child == null) throw new IOException("Windows could not start MoonLyrics.");
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            bool probe = Array.IndexOf(arguments, "--startup-probe") >= 0 || Array.IndexOf(arguments, "--self-test") >= 0;
            if (probe)
                Console.Error.WriteLine(error.ToString());
            else
                MessageBox.Show("MoonLyrics 无法启动。请把 exe 放到可以写入的普通文件夹后重试。\r\n\r\n" +
                    "MoonLyrics could not start. Move the EXE to a writable folder and try again.\r\n\r\n" + error.Message,
                    "MoonLyrics", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            Cleanup(extractionRoot, extraction);
        }
    }

    private static IEnumerable<string> CandidateRoots()
    {
        var roots = new List<string>();
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        // Honor per-process redirection used by managed or portable environments.
        string configured = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (!string.IsNullOrWhiteSpace(configured) && Path.IsPathRooted(configured)) local = configured;
        if (!string.IsNullOrEmpty(local)) roots.Add(Path.Combine(local, "MoonLyrics", "runtime"));
        try { roots.Add(Path.Combine(Path.GetTempPath(), "MoonLyrics-runtime")); }
        catch (Exception error)
        {
            if (!(error is IOException || error is UnauthorizedAccessException || error is ArgumentException)) throw;
        }
        roots.Add(Path.Combine(applicationDirectory, "MoonLyrics-data", "runtime"));
        var unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string root in roots)
        {
            string full = Path.GetFullPath(root);
            if (unique.Add(full)) yield return full;
        }
    }

    private static void ExtractPayload(string destination)
    {
        string prefix = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("MoonLyrics.Payload"))
        {
            if (resource == null) throw new InvalidDataException("The bundled application is missing.");
            using (var archive = new ZipArchive(resource, ZipArchiveMode.Read, false))
            {
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    // Payloads are generated by our build, but reject paths that
                    // could escape the private directory even in a damaged ZIP.
                    string target = Path.GetFullPath(Path.Combine(destination, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                    if (!target.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Invalid bundled application path.");
                    if (entry.FullName.EndsWith("/"))
                    {
                        Directory.CreateDirectory(target);
                        continue;
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    using (Stream input = entry.Open())
                    using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        input.CopyTo(output);
                }
            }
        }
    }

    // Windows command-line quoting, including embedded quotes and trailing slashes.
    private static string JoinArguments(string[] arguments)
    {
        var command = new StringBuilder();
        foreach (string argument in arguments)
        {
            if (command.Length > 0) command.Append(' ');
            command.Append('"');
            int slashes = 0;
            foreach (char character in argument)
            {
                if (character == '\\') { slashes++; continue; }
                command.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
                command.Append(character);
                slashes = 0;
            }
            command.Append('\\', slashes * 2);
            command.Append('"');
        }
        return command.ToString();
    }

    private static void Cleanup(string root, string directory)
    {
        if (string.IsNullOrEmpty(root) || string.IsNullOrEmpty(directory)) return;
        string full = Path.GetFullPath(directory);
        string prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        // Only remove the unique directory created by this launcher invocation.
        if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
            !Path.GetFileName(full).StartsWith("run-", StringComparison.Ordinal)) return;
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                if (Directory.Exists(full)) Directory.Delete(full, true);
                return;
            }
            catch (IOException) { Thread.Sleep(100 * (attempt + 1)); }
            catch (UnauthorizedAccessException) { return; }
        }
    }
}
