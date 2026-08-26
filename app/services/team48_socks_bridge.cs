using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

public sealed class Team48SocksBridge : IDisposable
{
    readonly TcpListener _listener;
    readonly string _upHost;
    readonly int _upPort;
    readonly byte[] _user;
    readonly byte[] _pass;
    volatile bool _stop;

    public int Port { get; private set; }

    public Team48SocksBridge(string upHost, int upPort, string user, string pass)
    {
        _upHost = upHost;
        _upPort = upPort;
        _user = Encoding.UTF8.GetBytes(user ?? "");
        _pass = Encoding.UTF8.GetBytes(pass ?? "");
        if (_user.Length > 255 || _pass.Length > 255)
            throw new ArgumentException("proxy auth too long");
        _listener = new TcpListener(IPAddress.Loopback, 0);
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        var thread = new Thread(AcceptLoop);
        thread.IsBackground = true;
        thread.Start();
    }

    public void Dispose()
    {
        _stop = true;
        try { _listener.Stop(); } catch { }
    }

    void AcceptLoop()
    {
        while (!_stop)
        {
            try
            {
                var client = _listener.AcceptTcpClient();
                var thread = new Thread(() => Serve(client));
                thread.IsBackground = true;
                thread.Start();
            }
            catch
            {
                if (_stop) break;
            }
        }
    }

    void Serve(TcpClient chrome)
    {
        TcpClient upstream = null;
        try
        {
            chrome.NoDelay = true;
            chrome.ReceiveTimeout = 30000;
            chrome.SendTimeout = 30000;
            var cin = chrome.GetStream();
            if (cin.ReadByte() != 5) return;
            var nMethods = cin.ReadByte();
            if (nMethods < 1) return;
            var methods = new byte[nMethods];
            ReadExact(cin, methods, nMethods);
            cin.Write(new byte[] { 5, 0 }, 0, 2);

            var head = new byte[4];
            ReadExact(cin, head, 4);
            if (head[0] != 5 || head[1] != 1)
            {
                cin.Write(new byte[] { 5, 7, 0, 1, 0, 0, 0, 0, 0, 0 }, 0, 10);
                return;
            }
            byte[] addr = ReadAddr(cin, head[3]);
            if (addr == null) return;
            var portb = new byte[2];
            ReadExact(cin, portb, 2);

            upstream = new TcpClient();
            upstream.NoDelay = true;
            upstream.ReceiveTimeout = 30000;
            upstream.SendTimeout = 30000;
            upstream.Connect(_upHost, _upPort);
            var uin = upstream.GetStream();
            uin.Write(new byte[] { 5, 1, 2 }, 0, 3);
            var greet = new byte[2];
            ReadExact(uin, greet, 2);
            if (greet[0] != 5 || greet[1] != 2)
                throw new Exception("upstream socks needs user/pass");
            var auth = new byte[3 + _user.Length + _pass.Length];
            auth[0] = 1;
            auth[1] = (byte)_user.Length;
            Buffer.BlockCopy(_user, 0, auth, 2, _user.Length);
            auth[2 + _user.Length] = (byte)_pass.Length;
            Buffer.BlockCopy(_pass, 0, auth, 3 + _user.Length, _pass.Length);
            uin.Write(auth, 0, auth.Length);
            var authReply = new byte[2];
            ReadExact(uin, authReply, 2);
            if (authReply[1] != 0)
                throw new Exception("upstream socks auth failed");

            uin.Write(head, 0, 4);
            uin.Write(addr, 0, addr.Length);
            uin.Write(portb, 0, 2);

            var replyHead = new byte[4];
            ReadExact(uin, replyHead, 4);
            byte[] replyAddr = ReadAddr(uin, replyHead[3]);
            if (replyAddr == null) return;
            var replyPort = new byte[2];
            ReadExact(uin, replyPort, 2);
            cin.Write(replyHead, 0, 4);
            cin.Write(replyAddr, 0, replyAddr.Length);
            cin.Write(replyPort, 0, 2);

            chrome.ReceiveTimeout = 0;
            chrome.SendTimeout = 0;
            upstream.ReceiveTimeout = 0;
            upstream.SendTimeout = 0;
            var pump = new Thread(() => Pump(cin, uin));
            pump.IsBackground = true;
            pump.Start();
            Pump(uin, cin);
        }
        catch
        {
        }
        finally
        {
            try { chrome.Close(); } catch { }
            try { if (upstream != null) upstream.Close(); } catch { }
        }
    }

    static byte[] ReadAddr(NetworkStream stream, byte atyp)
    {
        if (atyp == 1)
        {
            var addr = new byte[4];
            ReadExact(stream, addr, 4);
            return addr;
        }
        if (atyp == 3)
        {
            var len = stream.ReadByte();
            if (len < 1) return null;
            var addr = new byte[1 + len];
            addr[0] = (byte)len;
            ReadExact(stream, addr, 1, len);
            return addr;
        }
        if (atyp == 4)
        {
            var addr = new byte[16];
            ReadExact(stream, addr, 16);
            return addr;
        }
        return null;
    }

    static void Pump(NetworkStream from, NetworkStream to)
    {
        var buf = new byte[8192];
        try
        {
            int n;
            while ((n = from.Read(buf, 0, buf.Length)) > 0)
                to.Write(buf, 0, n);
        }
        catch
        {
        }
    }

    static void ReadExact(NetworkStream stream, byte[] buf, int len)
    {
        ReadExact(stream, buf, 0, len);
    }

    static void ReadExact(NetworkStream stream, byte[] buf, int offset, int len)
    {
        var got = 0;
        while (got < len)
        {
            var n = stream.Read(buf, offset + got, len - got);
            if (n <= 0) throw new Exception("eof");
            got += n;
        }
    }
}
