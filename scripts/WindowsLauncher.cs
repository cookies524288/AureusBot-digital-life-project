// Windows GUI entry point. The Python environment lives next to the launcher.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class WindowsLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        try
        {
            if (!File.Exists(python))
                throw new FileNotFoundException("请先在项目目录运行 uv sync。");
            if (args.Length > 1 || (args.Length == 1 && args[0] != "--fake"))
                throw new ArgumentException("启动器支持的参数为 --fake。");

            var info = new ProcessStartInfo(python, "-m aureus.cli.windows_launcher")
            {
                WorkingDirectory = root,
                UseShellExecute = false
            };
            if (args.Length == 1)
                info.Arguments += " --fake";
            using (Process process = Process.Start(info))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Aureus 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
