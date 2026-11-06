using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    // Windows' argv quoting rules must also handle quotes and trailing slashes.
    private static string Quoted(string value)
    {
        StringBuilder text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char item in value)
        {
            if (item == '\\') { ++slashes; continue; }
            text.Append('\\', item == '"' ? slashes * 2 + 1 : slashes);
            text.Append(item);
            slashes = 0;
        }
        return text.Append('\\', slashes * 2).Append('"').ToString();
    }

    [STAThread]
    public static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string app = Path.Combine(root, "app");
        try
        {
            StringBuilder command = new StringBuilder("-B -X utf8 ");
            command.Append(Quoted(Path.Combine(app, "portable_start.py")));
            foreach (string arg in args) { command.Append(' ').Append(Quoted(arg)); }
            ProcessStartInfo start = new ProcessStartInfo(
                Path.Combine(root, "runtime", "pythonw.exe"), command.ToString());
            start.WorkingDirectory = Environment.CurrentDirectory;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show("请完整解压 ZIP 后再打开程序。\n\n" + error.Message,
                            "B站投稿助手", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
