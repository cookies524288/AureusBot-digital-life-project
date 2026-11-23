using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;

internal sealed class LauncherWindow : IDisposable
{
    private readonly string page;
    private readonly string iconUrl;
    private readonly LauncherExitSignal exitSignal = new LauncherExitSignal();
    private bool ready;

    public LauncherWindow()
    {
        string directory = Path.Combine(Path.GetTempPath(), "Aureus-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        page = Path.Combine(directory, "index.html");
        using (var resource = typeof(LauncherWindow).Assembly.GetManifestResourceStream("Aureus.Tray.ico"))
        using (var bytes = new MemoryStream())
        {
            resource.CopyTo(bytes);
            iconUrl = "data:image/x-icon;base64," + Convert.ToBase64String(bytes.ToArray());
        }
        Status("Aureus 正在启动", "正在等待网页服务就绪。", "1");
        OpenTab(new Uri(page).AbsoluteUri);
    }

    private static void OpenTab(string url)
    {
        var info = new ProcessStartInfo(url) { UseShellExecute = true };
        using (Process opened = Process.Start(info)) { }
    }

    public void Ready(string url)
    {
        Status("Aureus 已启动", "正在打开主页面。", "0;url=" + exitSignal.TabUrl(url));
        ready = true;
    }

    public void Open(string url)
    {
        OpenTab(exitSignal.TabUrl(url));
    }

    public void Exited()
    {
        if (!ready) return;
        exitSignal.ConfirmExit();
        Status("Aureus 已退出", "可以关闭这个标签页。", null);
    }

    public void Failure(string error)
    {
        Status("Aureus 启动失败", error, null);
        if (ready) OpenTab(new Uri(page).AbsoluteUri);
    }

    private void Status(string title, string text, string refresh)
    {
        string markup = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            + "<link rel='icon' href='" + iconUrl + "'>"
            + (refresh == null ? "" : "<meta http-equiv='refresh' content='" + WebUtility.HtmlEncode(refresh) + "'>")
            + "<title>" + title + "</title><style>body{font:17px system-ui;margin:10vh auto;padding:24px;max-width:620px}p{white-space:pre-wrap}</style>"
            + "<img width='64' height='64' alt='' src='" + iconUrl + "'><h1>" + title + "</h1><p>" + WebUtility.HtmlEncode(text) + "</p></html>";
        string pending = page + ".next";
        File.WriteAllText(pending, markup, Encoding.UTF8);
        if (File.Exists(page)) File.Replace(pending, page, null);
        else File.Move(pending, page);
    }

    public void Dispose()
    {
        exitSignal.Dispose();
    }
}

internal sealed class LauncherExitSignal : IDisposable
{
    private readonly string token = Guid.NewGuid().ToString("N");
    private readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
    private readonly TaskCompletionSource<bool> stopped = new TaskCompletionSource<bool>();
    private readonly ConcurrentDictionary<string, byte> origins = new ConcurrentDictionary<string, byte>();
    private int port;

    public string TabUrl(string url)
    {
        if (port == 0)
        {
            listener.Start();
            port = ((IPEndPoint)listener.LocalEndpoint).Port;
            AcceptConnections();
        }
        origins.TryAdd(new Uri(url).GetLeftPart(UriPartial.Authority), 0);
        return url + "#aureus-launcher=" + port + "/" + token;
    }

    public void ConfirmExit()
    {
        stopped.TrySetResult(true);
    }

    private async void AcceptConnections()
    {
        try
        {
            while (true)
                WatchTab(await listener.AcceptTcpClientAsync().ConfigureAwait(false));
        }
        catch (ObjectDisposedException) { }
        catch (SocketException) { }
    }

    private async void WatchTab(TcpClient client)
    {
        using (client)
        using (NetworkStream stream = client.GetStream())
        using (var input = new StreamReader(stream, Encoding.ASCII))
        {
            try
            {
                string request = await input.ReadLineAsync().ConfigureAwait(false);
                if (request == null) return;
                bool preflight = request == "OPTIONS /" + token + " HTTP/1.1";
                if (!preflight && request != "GET /" + token + " HTTP/1.1") return;

                string host = null;
                string origin = null;
                string line;
                while (!String.IsNullOrEmpty(line = await input.ReadLineAsync().ConfigureAwait(false)))
                {
                    if (line.StartsWith("Host:", StringComparison.OrdinalIgnoreCase))
                        host = line.Substring(5).Trim();
                    if (line.StartsWith("Origin:", StringComparison.OrdinalIgnoreCase))
                        origin = line.Substring(7).Trim();
                }
                if (host != "127.0.0.1:" + port || origin == null || !origins.ContainsKey(origin)) return;

                // Keep the request open across restarts. An interrupted connection alone carries no exit signal.
                if (!preflight)
                {
                    Task ended = await Task.WhenAny(stopped.Task, input.ReadLineAsync()).ConfigureAwait(false);
                    if (ended != stopped.Task || !stopped.Task.Result) return;
                }
                string response = (preflight ? "HTTP/1.1 204 No Content\r\n" : "HTTP/1.1 200 OK\r\n")
                    + "Access-Control-Allow-Origin: " + origin + "\r\n"
                    + "Access-Control-Allow-Private-Network: true\r\n"
                    + "Cache-Control: no-store\r\nConnection: close\r\n"
                    + (preflight ? "\r\n" : "Content-Length: 6\r\n\r\nexited");
                byte[] bytes = Encoding.ASCII.GetBytes(response);
                await stream.WriteAsync(bytes, 0, bytes.Length).ConfigureAwait(false);
            }
            catch (IOException) { }
        }
    }

    public void Dispose()
    {
        // Give tabs already waiting, or connecting as the page finishes loading, time to read the result.
        if (stopped.Task.IsCompleted) System.Threading.Thread.Sleep(2000);
        listener.Stop();
        stopped.TrySetResult(false);
    }
}
