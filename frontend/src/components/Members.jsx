"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { guarded } from "../guard";

const ROLES = ["viewer", "editor", "approver", "admin"];

// People who may sign in to this organization, and their roles (multi-tenant mode).
export default function Members() {
  const [members, setMembers] = useState(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");

  const load = useCallback(() => guarded(async () => setMembers(await api("org/members")))(), []);
  useEffect(() => {
    load();
  }, [load]);

  const add = guarded(async () => {
    await api("org/members", "POST", { email, role });
    setEmail("");
    await load();
  });
  const remove = (address) => guarded(async () => {
    await api("org/members/" + encodeURIComponent(address), "DELETE");
    await load();
  });

  return (
    <div className="card stack" id="members">
      <h2>People</h2>
      {members && (
        <table>
          <thead><tr><th>E-mail</th><th>Role</th><th /></tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.email}>
                <td>{m.email}</td>
                <td>{m.role}</td>
                <td><button className="danger" onClick={remove(m.email)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        <input placeholder="colleague@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        <select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value)}>
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button className="primary" onClick={add}>Add person</button>
      </div>
    </div>
  );
}
