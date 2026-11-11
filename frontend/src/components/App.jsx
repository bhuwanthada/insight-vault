"use client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { NEXT_THEME, applyTheme, saveTheme, storedTheme } from "../theme";
import Login from "./Login";
import Workspace from "./Workspace";

// Loads the public settings and the sign-in state, then shows either the login screen or the workspace.
export default function App() {
  const [config, setConfig] = useState(null);
  const [auth, setAuth] = useState(null);
  const [failed, setFailed] = useState(false);
  const [theme, setTheme] = useState("system");

  useEffect(() => {
    Promise.all([api("config/public"), api("auth/me")])
      .then(([c, a]) => {
        setConfig(c);
        setAuth(a);
        setTheme(storedTheme() || c.defaultTheme);
        document.title = c.appName;
        document.documentElement.style.setProperty("--accent", c.accentColor);
      })
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggleTheme = () => {
    const next = NEXT_THEME[theme];
    saveTheme(next);
    setTheme(next);
  };

  if (failed) return <div className="card alert" role="alert">Cannot reach the API. Is the backend running?</div>;
  if (!config || !auth) return null;
  if (!auth.authenticated) return <Login config={config} />;
  return <Workspace config={config} user={auth.user} theme={theme} onToggleTheme={toggleTheme} />;
}
