// Windows GUI entry point. The Python environment lives next to the launcher.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
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

            var info = new ProcessStartInfo(python, "-m aureus.cli.windows_launcher --tray")
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true
            };
            if (args.Length == 1)
                info.Arguments += " --fake";
            using (Process process = Process.Start(info))
            {
                RunTray(process);
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

    private static void RunTray(Process process)
    {
        const string urlPrefix = "AUREUS_WEB_URL=";
        string webUrl = null;
        using (var context = new ApplicationContext())
        using (var icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath))
        using (var tray = new NotifyIcon())
        using (var menu = new ContextMenuStrip())
        using (var timer = new System.Windows.Forms.Timer { Interval = 200 })
        {
            var exit = menu.Items.Add("退出");
            exit.Click += delegate
            {
                exit.Enabled = false;
                process.StandardInput.Close();
            };
            tray.Icon = icon ?? SystemIcons.Application;
            tray.Text = "AureusBot";
            tray.ContextMenuStrip = menu;
            tray.MouseClick += delegate(object sender, MouseEventArgs args)
            {
                string url = Volatile.Read(ref webUrl);
                if (args.Button == MouseButtons.Left && exit.Enabled && url != null)
                    using (Process.Start(new ProcessStartInfo(url) { UseShellExecute = true })) { }
            };
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data == null || !args.Data.StartsWith(urlPrefix, StringComparison.Ordinal))
                    return;
                Uri url;
                if (Uri.TryCreate(args.Data.Substring(urlPrefix.Length), UriKind.Absolute, out url)
                    && url.Scheme == Uri.UriSchemeHttp && url.Host == "127.0.0.1")
                    Volatile.Write(ref webUrl, url.AbsoluteUri);
            };
            process.BeginOutputReadLine();
            timer.Tick += delegate
            {
                if (process.HasExited)
                    context.ExitThread();
                else if (!tray.Visible && Volatile.Read(ref webUrl) != null)
                    tray.Visible = true;
            };
            timer.Start();
            Application.Run(context);
        }
    }
}
