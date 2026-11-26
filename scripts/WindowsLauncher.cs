// Windows GUI entry point. The Python environment lives next to the launcher.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class WindowsLauncher
{
    private const int RestartExitCode = 75;
    // 托盘点了重启/退出后，子进程超过宽限还没退出就结束它（D144）。
    private const int RestartGraceSeconds = 180;
    private const int ExitGraceSeconds = 30;

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, @".venv\Scripts\pythonw.exe");
        try
        {
            using (var window = new LauncherWindow())
                return Launch(args, root, python, window);
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Aureus 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int Launch(string[] args, string root, string python, LauncherWindow window)
    {
        try
        {
            if (!File.Exists(python))
                throw new FileNotFoundException("请先在项目目录运行 uv sync。");
            if (args.Length > 1 || (args.Length == 1 && args[0] != "--fake"))
                throw new ArgumentException("启动器支持的参数为 --fake。");

            var info = new ProcessStartInfo(python, "-m aureus.cli.windows_launcher --tray --owner-pid " + Process.GetCurrentProcess().Id)
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true
            };
            if (args.Length == 1)
                info.Arguments += " --fake";
            string arguments = info.Arguments;
            while (true)
            {
                using (Process process = Process.Start(info))
                {
                    var status = new ChildStatus();
                    RunTray(process, window, status);
                    process.WaitForExit();
                    if (status.Attached)
                    {
                        Process owner;
                        try { owner = Process.GetProcessById(status.Owner); }
                        catch (ArgumentException)
                        {
                            window.Failure("正在运行的 Aureus 实例已退出，请重新打开。");
                            return 1;
                        }
                        using (owner)
                        {
                            window.Ready(status.Url);
                            owner.WaitForExit();
                            window.Exited();
                        }
                        return 0;
                    }
                    if (status.KilledForExit)
                    {
                        window.Exited();
                        return 0;
                    }
                    if (process.ExitCode != 0 && process.ExitCode != RestartExitCode && status.Url == null
                        && !status.KilledForRestart)
                        window.Failure(status.Error ?? ("网页服务启动失败，退出码 " + process.ExitCode + "。"));
                    if (process.ExitCode != RestartExitCode && !status.KilledForRestart)
                    {
                        if (process.ExitCode == 0 || status.Url != null)
                            window.Exited();
                        return process.ExitCode;
                    }
                }
                info.Arguments = arguments + " --restarted";
            }
        }
        catch (Exception error)
        {
            window.Failure(error.Message);
            return 1;
        }
    }

    private sealed class ChildStatus
    {
        public string Url;
        public string Error;
        public int Owner;
        public bool Attached;
        public bool KilledForRestart;
        public bool KilledForExit;
    }

    private static string LocalUrl(string value)
    {
        Uri url;
        return Uri.TryCreate(value, UriKind.Absolute, out url)
            && url.Scheme == Uri.UriSchemeHttp && url.Host == "127.0.0.1" ? url.AbsoluteUri : null;
    }

    private static void KillProcessTree(Process process)
    {
        try
        {
            var info = new ProcessStartInfo("taskkill", "/T /F /PID " + process.Id)
            {
                UseShellExecute = false,
                CreateNoWindow = true
            };
            using (Process.Start(info)) { }
        }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
    }

    private static void RunTray(Process process, LauncherWindow window, ChildStatus status)
    {
        const string urlPrefix = "AUREUS_WEB_URL=";
        const string attachPrefix = "AUREUS_WEB_ATTACH=";
        const string errorPrefix = "AUREUS_WEB_ERROR=";
        using (var context = new ApplicationContext())
        using (var stream = System.Reflection.Assembly.GetExecutingAssembly().GetManifestResourceStream("Aureus.Tray.ico"))
        using (var icon = new Icon(stream))
        using (var smallIcon = new Icon(icon, SystemInformation.SmallIconSize))
        using (var menuIcon = smallIcon.ToBitmap())
        using (var tray = new NotifyIcon())
        using (var menu = new ContextMenuStrip())
        using (var timer = new System.Windows.Forms.Timer { Interval = 200 })
        {
            Stopwatch restartRequestedAt = null;
            Stopwatch exitRequestedAt = null;
            Action openPage = delegate
            {
                string url = Volatile.Read(ref status.Url);
                if (url != null && !process.HasExited)
                    window.Open(url);
            };
            var open = menu.Items.Add("打开主页面", menuIcon);
            open.Click += delegate { openPage(); };
            var restart = menu.Items.Add("重启");
            var exit = menu.Items.Add("退出");
            restart.Click += delegate
            {
                restart.Enabled = false;
                exit.Enabled = false;
                open.Enabled = false;
                restartRequestedAt = Stopwatch.StartNew();
                if (!process.HasExited)
                {
                    process.StandardInput.BaseStream.WriteByte((byte)'R');
                    process.StandardInput.BaseStream.Flush();
                }
            };
            exit.Click += delegate
            {
                exit.Enabled = false;
                restart.Enabled = false;
                open.Enabled = false;
                exitRequestedAt = Stopwatch.StartNew();
                process.StandardInput.Close();
            };
            tray.Icon = smallIcon;
            tray.Text = "AureusBot";
            tray.ContextMenuStrip = menu;
            tray.MouseClick += delegate(object sender, MouseEventArgs args)
            {
                if (args.Button == MouseButtons.Left && open.Enabled)
                    openPage();
            };
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                string value = args.Data;
                if (value == null) return;
                if (value.StartsWith(urlPrefix, StringComparison.Ordinal))
                    Volatile.Write(ref status.Url, LocalUrl(value.Substring(urlPrefix.Length)));
                else if (value.StartsWith(attachPrefix, StringComparison.Ordinal))
                {
                    string[] fields = value.Substring(attachPrefix.Length).Split(new[] { ' ' }, 2);
                    int owner;
                    if (fields.Length == 2 && int.TryParse(fields[0], out owner) && owner > 0 && LocalUrl(fields[1]) != null)
                    {
                        status.Owner = owner;
                        status.Attached = true;
                        Volatile.Write(ref status.Url, LocalUrl(fields[1]));
                    }
                }
                else if (value.StartsWith(errorPrefix, StringComparison.Ordinal))
                    status.Error = Encoding.UTF8.GetString(Convert.FromBase64String(value.Substring(errorPrefix.Length)));
            };
            process.BeginOutputReadLine();
            timer.Tick += delegate
            {
                if (process.HasExited)
                {
                    context.ExitThread();
                    return;
                }
                if (restartRequestedAt != null
                    && restartRequestedAt.Elapsed.TotalSeconds >= RestartGraceSeconds)
                {
                    restartRequestedAt = null;
                    status.KilledForRestart = true;
                    KillProcessTree(process);
                }
                else if (exitRequestedAt != null
                    && exitRequestedAt.Elapsed.TotalSeconds >= ExitGraceSeconds)
                {
                    exitRequestedAt = null;
                    status.KilledForExit = true;
                    KillProcessTree(process);
                }
                if (!tray.Visible && Volatile.Read(ref status.Url) != null && !status.Attached)
                {
                    window.Ready(status.Url);
                    tray.Visible = true;
                }
            };
            timer.Start();
            Application.Run(context);
        }
    }
}
