using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;

internal sealed class LauncherWindow : IDisposable
{
    private readonly string directory;
    private readonly string page;
    private readonly string browser;
    private Process process;
    private bool ready;

    public LauncherWindow()
    {
        browser = FindBrowser();
        directory = Path.Combine(Path.GetTempPath(), "Aureus-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        page = Path.Combine(directory, "index.html");
        Status("Aureus 正在启动", "正在等待网页服务就绪。", "1");
        Open(new Uri(page).AbsoluteUri);
    }

    private static string FindBrowser()
    {
        foreach (string folder in new[] { Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData) })
        {
            foreach (string relative in new[] { "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe" })
            {
                string candidate = Path.Combine(folder, relative);
                if (File.Exists(candidate)) return candidate;
            }
        }
        throw new FileNotFoundException("请安装 Microsoft Edge 或 Google Chrome，以打开 Aureus 专用窗口。");
    }

    public void Open(string url)
    {
        char quote = (char)34;
        var info = new ProcessStartInfo(browser, "--no-first-run --no-default-browser-check "
            + "--disable-background-mode --disable-background-networking "
            + "--user-data-dir=" + quote + Path.Combine(directory, "profile") + quote
            + " --app=" + quote + url + quote)
        { UseShellExecute = false };
        Process opened = Process.Start(info);
        if (process == null || process.HasExited)
        {
            if (process != null) process.Dispose();
            process = opened;
        }
        else opened.Dispose();
    }

    public void Ready(string url)
    {
        Status("Aureus 已启动", "正在打开主页面。", "0;url=" + url);
        ready = true;
    }

    public void Failure(string error)
    {
        Status("Aureus 启动失败", error, null);
        if (ready) Open(new Uri(page).AbsoluteUri);
        if (process != null) process.WaitForExit();
    }

    private void Status(string title, string text, string refresh)
    {
        string markup = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            + (refresh == null ? "" : "<meta http-equiv='refresh' content='" + WebUtility.HtmlEncode(refresh) + "'>")
            + "<title>" + title + "</title><style>body{font:17px system-ui;margin:10vh auto;padding:24px;max-width:620px}p{white-space:pre-wrap}</style>"
            + "<h1>" + title + "</h1><p>" + WebUtility.HtmlEncode(text) + "</p></html>";
        string pending = page + ".next";
        File.WriteAllText(pending, markup, Encoding.UTF8);
        if (File.Exists(page)) File.Replace(pending, page, null);
        else File.Move(pending, page);
    }

    public void Dispose()
    {
        if (process == null) return;
        if (!process.HasExited)
        {
            uint pid = (uint)process.Id;
            EnumWindows(delegate(IntPtr window, IntPtr ignored)
            {
                uint owner;
                GetWindowThreadProcessId(window, out owner);
                if (owner == pid && IsWindowVisible(window)) PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero);
                return true;
            }, IntPtr.Zero);
        }
        if (!process.WaitForExit(2000))
        {
            // The launcher created this process with a unique profile. Chromium may
            // keep it running after every window has accepted WM_CLOSE.
            try { process.Kill(); }
            catch (InvalidOperationException) { }
            process.WaitForExit(2000);
        }
        if (process.HasExited)
        {
            try { Directory.Delete(directory, true); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        process.Dispose();
    }

    private delegate bool WindowCallback(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(WindowCallback callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wparam, IntPtr lparam);
}
