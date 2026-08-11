import os
import shutil
import socket
import sys
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

import tkinter as tk


def human_size(num):
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(num)} B"
            return f"{num:.1f} {unit}"
        num /= 1024


def content_disposition(name):
    fallback = (
        name.encode("latin-1", "replace").decode("latin-1").replace('"', "'").replace("\\", "_")
    )
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        encoded = quote(name.encode("utf-8"))
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
    safe = name.replace('"', "'").replace("\\", "_")
    return f"attachment; filename=\"{safe}\""


def get_local_ips():
    result = ["127.0.0.1"]
    seen = {"127.0.0.1"}

    def add(ip):
        if ip and ip not in seen:
            seen.add(ip)
            result.append(ip)

    try:
        add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return result


class SingleFileHandler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.app is None or not self.app.running:
            self.send_error(503, "Server is off")
            return
        if self.path in ("/", "/index.html"):
            self.serve_index()
        elif self.path == "/download":
            self.serve_file()
        else:
            self.send_error(404, "Not found")

    def serve_index(self):
        name = os.path.basename(self.app.file_path)
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Single File Server</title></head><body>"
            "<h2>File ready</h2>"
            f"<p><b>{escape(name)}</b> &mdash; "
            f"{human_size(os.path.getsize(self.app.file_path))}</p>"
            '<p><a href="/download">DOWNLOAD</a></p>'
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self):
        file_path = self.app.file_path
        try:
            with open(file_path, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition",
                    content_disposition(os.path.basename(file_path)),
                )
                self.send_header("Content-Length", str(os.path.getsize(file_path)))
                self.end_headers()
                shutil.copyfileobj(f, self.wfile)
        except OSError:
            self.send_error(404, "File not found")


class App:
    def __init__(self, root, cli_file=None, cli_port=None):
        self.root = root
        self.server = None
        self.server_thread = None
        self.running = False
        self.file_path = None
        self.port = None

        self.path_var = tk.StringVar(value=cli_file or "")
        self.port_var = tk.StringVar(value=str(cli_port) if cli_port else "8080")
        self.status_var = tk.StringVar(value="Server stopped")

        root.title("Single File Server")

        frame = tk.Frame(root, padx=8, pady=8)
        frame.pack(fill="both", expand=True)

        frame.columnconfigure(1, weight=1)

        tk.Label(frame, text="File path:").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=6
        )
        self.path_entry = tk.Entry(frame, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.path_warn = tk.Label(frame, text="", fg="red")
        self.path_warn.grid(row=0, column=2, sticky="w", padx=6, pady=6)

        tk.Label(frame, text="Port:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=6)
        self.port_entry = tk.Entry(frame, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=1, column=1, sticky="w", pady=6)
        self.port_warn = tk.Label(frame, text="", fg="red")
        self.port_warn.grid(row=1, column=2, sticky="w", padx=6, pady=6)

        tk.Label(frame, text="IP addresses:").grid(
            row=2, column=0, sticky="nw", padx=(0, 6), pady=6
        )
        self.ip_text = tk.Text(frame, height=4, width=40, wrap="none")
        self.ip_text.grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)
        self.ip_text.config(state="disabled")

        self.toggle_btn = tk.Button(frame, text="Turn ON", width=12, command=self.toggle)
        self.toggle_btn.grid(row=3, column=0, sticky="w", padx=(0, 6), pady=6)
        tk.Label(frame, textvariable=self.status_var).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=6
        )

        self.path_var.trace_add("write", lambda *a: self.update_warnings())
        self.port_var.trace_add("write", lambda *a: self.update_warnings())

        self.refresh_ips()
        self.update_warnings()

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if cli_file is not None or cli_port is not None:
            self.root.after(100, self.toggle)

    def validate(self):
        issues = {}
        path = self.path_var.get().strip()
        if not path:
            issues["path"] = "Please enter a file path"
        elif not os.path.isfile(path):
            issues["path"] = "File does not exist"
        else:
            self.file_path = os.path.abspath(path)

        raw_port = self.port_var.get().strip()
        if not raw_port.isdigit():
            issues["port"] = "Port must be a number"
        else:
            port = int(raw_port)
            if not (1 <= port <= 65535):
                issues["port"] = "Port must be 1-65535"
            else:
                self.port = port
        return issues

    def update_warnings(self):
        issues = self.validate()
        self.path_warn.config(text=issues.get("path", ""))
        self.port_warn.config(text=issues.get("port", ""))

    def refresh_ips(self):
        lines = "\n".join(get_local_ips()) or "none"
        self.ip_text.config(state="normal")
        self.ip_text.delete("1.0", "end")
        self.ip_text.insert("1.0", lines)
        self.ip_text.config(state="disabled")

    def start_server(self):
        issues = self.validate()
        if issues:
            self.update_warnings()
            return False
        try:
            SingleFileHandler.app = self
            self.server = HTTPServer(("0.0.0.0", self.port), SingleFileHandler)
        except OSError:
            self.port_warn.config(text=f"Port {self.port} is already in use")
            return False
        self.running = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.refresh_ips()
        self.update_toggle_ui()
        return True

    def stop_server(self):
        if self.server is not None:
            srv, self.server_thread = self.server, None
            self.server = None
            self.running = False
            srv.shutdown()
            srv.server_close()
        self.update_toggle_ui()

    def toggle(self):
        if self.running:
            self.stop_server()
        else:
            self.start_server()

    def update_toggle_ui(self):
        if self.running:
            self.toggle_btn.config(
                text="Turn OFF", bg="#c8f7c5", activebackground="#b0e6ae"
            )
            self.status_var.set(f"Server running on port {self.port}")
        else:
            self.toggle_btn.config(text="Turn ON", bg="SystemButtonFace", activebackground="SystemButtonFace")
            self.status_var.set("Server stopped")

    def on_close(self):
        if self.server is not None:
            self.stop_server()
        self.root.destroy()


def parse_args(argv):
    file_path = None
    port = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print("Usage: python single_file_server.py [FILE] [-p PORT]")
            sys.exit(0)
        if arg in ("-p", "--port"):
            i += 1
            if i >= len(argv):
                print("Missing value for -p/--port")
                sys.exit(2)
            try:
                port = int(argv[i])
            except ValueError:
                print(f"Invalid port: {argv[i]}")
                sys.exit(2)
        elif arg.startswith("--port="):
            try:
                port = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"Invalid port: {arg}")
                sys.exit(2)
        elif arg.startswith("-p") and len(arg) > 2:
            try:
                port = int(arg[2:])
            except ValueError:
                print(f"Invalid port: {arg}")
                sys.exit(2)
        elif arg.startswith("-"):
            print(f"Unknown option: {arg}")
            sys.exit(2)
        else:
            if file_path is not None:
                print("Only one file path is allowed")
                sys.exit(2)
            file_path = arg
        i += 1
    return file_path, port


def main(argv):
    file_path, port = parse_args(argv)
    root = tk.Tk()
    App(root, cli_file=file_path, cli_port=port)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])