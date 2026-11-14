using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("term-rewind")]
[assembly: AssemblyProduct("term-rewind")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class Launcher
{
    [STAThread]
    private static void Main(string[] args)
    {
        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string runtime = Path.Combine(directory, "runtime");
        string python = Path.Combine(runtime, "pythonw.exe");
        string script = Path.Combine(directory, "app.pyw");
        try
        {
            if (!File.Exists(python) || !File.Exists(script))
                throw new FileNotFoundException("请在 term-rewind 应用目录中启动 term-rewind.exe。");

            string arguments = (char)34 + script + (char)34;
            if (Array.IndexOf(args, "--demo") >= 0)
                arguments += " --demo";

            var info = new ProcessStartInfo(python, arguments);
            info.WorkingDirectory = directory;
            info.UseShellExecute = false;
            info.EnvironmentVariables["TCL_LIBRARY"] = Path.Combine(runtime, "tcl", "tcl8.6");
            info.EnvironmentVariables["TK_LIBRARY"] = Path.Combine(runtime, "tcl", "tk8.6");
            using (Process process = Process.Start(info)) { }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "term-rewind", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
