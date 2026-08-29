import React, { useEffect, useState } from "react";
import {
  Activity,
  BookOpenText,
  Bot,
  Boxes,
  ExternalLink,
  Heart,
  LogOut,
  Octagon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  ShieldCheck,
} from "lucide-react";
import { api, getToken, setToken } from "./api.js";
import StatusPanel from "./components/StatusPanel.jsx";
import LogViewer from "./components/LogViewer.jsx";
import ConfigPanel from "./components/ConfigPanel.jsx";
import AccountsPanel from "./components/AccountsPanel.jsx";
import CodeBuddyPanel from "./components/CodeBuddyPanel.jsx";
import { Badge, Button, Card, Input, Spinner } from "./components/ui.jsx";

const NAV = [
  { id: "status", label: "Status", icon: Activity },
  { id: "log", label: "Live Log", icon: BookOpenText },
  { id: "config", label: "Config", icon: Settings },
  { id: "accounts", label: "Accounts", icon: Boxes },
  { id: "codebuddy", label: "CodeBuddy", icon: Bot },
];

export default function App() {
  const [auth, setAuth] = useState(null);
  const [tab, setTab] = useState("status");
  const [password, setPassword] = useState("");
  const [running, setRunning] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  useEffect(() => {
    api
      .get("/api/config")
      .then((d) => setAuth({ needs: d.needs_auth }))
      .catch(() => setAuth({ needs: true }));
  }, []);
  useEffect(() => {
    if (auth?.needs && !getToken()) return undefined;
    const timer = setInterval(
      () =>
        api
          .get("/api/status")
          .then((d) => setRunning(!!d.running))
          .catch(() => {}),
      2500,
    );
    return () => clearInterval(timer);
  }, [auth]);

  async function doLogin() {
    try {
      const data = await api.post("/api/auth", { password });
      setToken(data.token);
      setAuth({ needs: data.needs_auth });
      setPassword("");
    } catch (error) {
      alert(`Login failed: ${error.message}`);
    }
  }

  if (auth === null)
    return (
      <main className="app-loading">
        <Spinner />
        <span>Loading application</span>
      </main>
    );
  if (auth.needs && !getToken())
    return (
      <main className="app-login">
        <Card className="app-login-card">
          <div className="app-login-mark">
            <ShieldCheck size={26} />
          </div>
          <h1>GitHub Register</h1>
          <p>Enter the access password to open the console.</p>
          <Input
            type="password"
            placeholder="Access password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doLogin()}
          />
          <Button variant="primary" size="lg" onClick={doLogin}>
            Sign in
          </Button>
        </Card>
      </main>
    );

  const ActivePanel = {
    status: StatusPanel,
    log: LogViewer,
    config: ConfigPanel,
    accounts: AccountsPanel,
    codebuddy: CodeBuddyPanel,
  }[tab];
  return (
    <div
      className={sidebarOpen ? "app-shell" : "app-shell app-shell-collapsed"}
    >
      <aside
        className={
          sidebarOpen ? "app-sidebar" : "app-sidebar app-sidebar-collapsed"
        }
      >
        <div className="app-sidebar-top">
          <div className="app-brand">
            <div className="app-brand-icon">
              <Octagon size={20} />
            </div>
            <div className="app-brand-copy">
              <strong>GitHub Register</strong>
              <a
                className="app-brand-link"
                href="https://github.com/mhiqrambg/github-regkit-mibp"
                target="_blank"
                rel="noreferrer"
              >
                MIBP DEV
              </a>
            </div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="app-sidebar-toggle"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            {sidebarOpen ? (
              <PanelLeftClose size={17} />
            ) : (
              <PanelLeftOpen size={17} />
            )}
          </Button>
        </div>
        <nav className="app-nav">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={tab === id ? "app-nav-item active" : "app-nav-item"}
              onClick={() => setTab(id)}
              title={label}
            >
              <Icon size={17} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="app-sidebar-footer">
          <Badge tone={running ? "success" : "muted"}>
            {running && <span className="pulse-dot" />}
            {running ? "Job running" : "Idle"}
          </Badge>
          {auth.needs && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setToken("");
                window.location.reload();
              }}
            >
              <LogOut size={14} /> <span>Sign out</span>
            </Button>
          )}
          <a
            className="app-support"
            href="https://trakteer.id/mhiqrambg/tip"
            target="_blank"
            rel="noreferrer"
            title="Support on Trakteer"
          >
            <Heart size={12} />
            <span>Support</span>
          </a>
          <a
            className="app-credit"
            href="https://github.com/mhiqrambg/github-regkit-mibp"
            target="_blank"
            rel="noreferrer"
            title="mhiqrambg/github-regkit-mibp"
          >
            <ExternalLink size={12} />
            <span>mhiqrambg/github-regkit-mibp</span>
          </a>
        </div>
      </aside>
      <main className="app-main" key={tab}>
        <ActivePanel
          onGotoLogs={() => setTab("log")}
          onGotoAccounts={() => setTab("accounts")}
        />
      </main>
    </div>
  );
}
