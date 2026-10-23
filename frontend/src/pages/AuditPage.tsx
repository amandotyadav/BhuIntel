import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { projectPath, useAudit } from "../api/hooks";
import { Button, Card, ErrorBanner, Loading, Notice, Table, Td, fmtDate } from "../components/ui";
import { useProjectId } from "./ProjectLayout";

export function AuditPage() {
  const pid = useProjectId();
  const audit = useAudit(pid);
  const [check, setCheck] = useState(false);
  const verify = useQuery({
    queryKey: ["audit-verify", pid],
    queryFn: () => api.get<{ ok: boolean; events_checked: number; first_invalid_event_id: number | null }>(`${projectPath(pid)}/audit/verify`),
    enabled: check,
  });
  return (
    <Card title="Audit trail (append-only, hash-chained)" actions={<Button variant="secondary" onClick={() => { setCheck(true); void verify.refetch(); }}>Verify chain</Button>}>
      {verify.data && (verify.data.ok
        ? <Notice tone="green">Hash chain intact: {verify.data.events_checked} events verified.</Notice>
        : <Notice tone="amber">Chain broken at event #{verify.data.first_invalid_event_id}.</Notice>)}
      {audit.isLoading && <Loading />}
      <ErrorBanner error={audit.error || verify.error} />
      <Table head={["#", "Time", "Actor", "Action", "Entity", "Details", "Request", "Hash"]} empty={audit.data?.length === 0}>
        {audit.data?.map((e) => (
          <tr key={e.id}><Td>{e.id}</Td><Td className="whitespace-nowrap text-xs">{fmtDate(e.ts)}</Td><Td className="text-xs">{e.actor ?? "system"}</Td>
            <Td className="font-mono text-xs">{e.action}</Td><Td className="text-xs">{e.entity_type} {e.entity_id}</Td>
            <Td className="max-w-sm truncate font-mono text-xs" >{JSON.stringify(e.details)}</Td>
            <Td className="font-mono text-xs">{e.request_id?.slice(0, 8)}</Td><Td className="font-mono text-xs">{e.hash.slice(0, 10)}…</Td></tr>
        ))}
      </Table>
    </Card>
  );
}
