import {
  connectControlDeckEvents,
  type ControlDeckConvergenceEntry,
  type ControlDeckSession,
  type ControlDeckSnapshot,
  type ControlDeckTask,
  type ControlDeckTaskReadiness,
} from "@tura/gateway-sdk";
import RefreshCw from "lucide-solid/icons/refresh-cw";
import { For, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js";
import { useGlobalGateway } from "../../context/gateway";

function shortIdentity(value: string | null | undefined, length = 12) {
  if (!value) return "--";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

function formatTime(value: number) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function convergenceRows(entry: ControlDeckConvergenceEntry) {
  const projection = entry.projection;
  return {
    receipts: projection?.receipts ?? [],
    callbacks: projection?.callbacks ?? [],
    continuations: projection?.continuations ?? [],
  };
}

export function ControlDeckView() {
  const { rootClient, gatewayUrl } = useGlobalGateway();
  const [snapshot, setSnapshot] = createSignal<ControlDeckSnapshot>();
  const [loading, setLoading] = createSignal(true);
  const [error, setError] = createSignal<string>();
  let refreshRevision = 0;

  async function refresh() {
    const requestRevision = ++refreshRevision;
    setLoading(true);
    try {
      const nextSnapshot = await rootClient().controlDeck();
      if (requestRevision !== refreshRevision) return;
      setSnapshot(nextSnapshot);
      setError(undefined);
    } catch (reason) {
      if (requestRevision !== refreshRevision) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (requestRevision === refreshRevision) setLoading(false);
    }
  }

  onMount(() => {
    void refresh();
    const stream = connectControlDeckEvents({
      baseUrl: gatewayUrl(),
      onChange: (event) => {
        if (event.state_head !== snapshot()?.state_head) void refresh();
      },
      onUnavailable: setError,
      onError: () => undefined,
    });
    onCleanup(() => stream.close());
  });

  const activeSessions = createMemo(
    () => snapshot()?.sessions.filter((session) => !session.terminal) ?? [],
  );
  const activeLeases = createMemo(
    () => snapshot()?.runtimes.filter((runtime) => runtime.lease_active) ?? [],
  );
  const readyTasks = createMemo(
    () => snapshot()?.ready_set?.filter((entry) => entry.state === "ready") ?? [],
  );
  const readinessByTask = createMemo(() => {
    const entries = new Map<string, ControlDeckTaskReadiness>();
    for (const entry of snapshot()?.ready_set ?? []) {
      entries.set(`${entry.session_id}:${entry.task_id}`, entry);
    }
    return entries;
  });
  const pendingCallbacks = createMemo(
    () =>
      snapshot()?.convergence.flatMap((entry) =>
        convergenceRows(entry).callbacks.filter((callback) => callback.stage !== "acknowledged"),
      ) ?? [],
  );
  const activeQueueRows = createMemo<
    Array<{ session: ControlDeckSession; task?: ControlDeckTask }>
  >(() => {
    const rows: Array<{ session: ControlDeckSession; task?: ControlDeckTask }> = [];
    for (const session of activeSessions()) {
      if (session.tasks.length === 0) {
        rows.push({ session });
        continue;
      }
      for (const task of session.tasks) rows.push({ session, task });
    }
    return rows;
  });

  return (
    <main class="control-deck-view">
      <header class="control-deck-header">
        <div>
          <p class="control-deck-kicker">COMMANDER / TURA</p>
          <h1>Control Deck</h1>
        </div>
        <div class="control-deck-header-actions">
          <Show when={snapshot()}>
            {(value) => (
              <span class={`control-deck-freshness ${value().freshness_status}`}>
                {value().freshness_status}
              </span>
            )}
          </Show>
          <button
            class="control-deck-icon-button"
            type="button"
            title="Refresh authoritative state"
            aria-label="Refresh authoritative state"
            disabled={loading()}
            onClick={() => void refresh()}
          >
            <RefreshCw size={16} class={loading() ? "spinning" : undefined} />
          </button>
        </div>
      </header>

      <Show when={error()}>{(message) => <div class="control-deck-alert">{message()}</div>}</Show>

      <Show
        when={snapshot()}
        fallback={<div class="control-deck-empty">Loading current state…</div>}
      >
        {(value) => (
          <>
            <section class="control-deck-metrics" aria-label="Control deck summary">
              <div>
                <strong>{value().missions.length}</strong>
                <span>Missions</span>
              </div>
              <div>
                <strong>{activeSessions().length}</strong>
                <span>Active sessions</span>
              </div>
              <div>
                <strong>{activeLeases().length}</strong>
                <span>Active leases</span>
              </div>
              <div>
                <strong>{pendingCallbacks().length}</strong>
                <span>Pending callbacks</span>
              </div>
              <div>
                <strong>{readyTasks().length}</strong>
                <span>Ready tasks</span>
              </div>
            </section>

            <section class="control-deck-band">
              <div class="control-deck-section-title">
                <h2>Mission DAG</h2>
                <span>{shortIdentity(value().state_head, 16)}</span>
              </div>
              <div class="control-deck-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Session</th>
                      <th>Parent</th>
                      <th>State</th>
                      <th>Tasks</th>
                      <th>Runtime</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={value().sessions}>
                      {(session) => (
                        <tr>
                          <td>
                            <code>{shortIdentity(session.session_id)}</code>
                            <small>
                              {session.name_sha256
                                ? `name:${shortIdentity(session.name_sha256)}`
                                : "Untitled"}
                            </small>
                          </td>
                          <td>
                            <code>{shortIdentity(session.parent_id)}</code>
                          </td>
                          <td>
                            <span class={`control-deck-state ${session.state}`}>
                              {session.state}
                            </span>
                          </td>
                          <td>{session.tasks.length}</td>
                          <td>
                            <code>{shortIdentity(session.active_runtime_id)}</code>
                          </td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
            </section>

            <section class="control-deck-band">
              <div class="control-deck-section-title">
                <h2>Active Queue</h2>
                <span>{activeSessions().length} current</span>
              </div>
              <div class="control-deck-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Session</th>
                      <th>Task</th>
                      <th>Start condition</th>
                      <th>Status</th>
                      <th>Readiness</th>
                      <th>Reason</th>
                      <th>Scope</th>
                      <th>Lease / workers</th>
                      <th>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={activeQueueRows()}>
                      {(row) => {
                        const readiness = () =>
                          row.task
                            ? readinessByTask().get(`${row.session.session_id}:${row.task.task_id}`)
                            : undefined;
                        return (
                          <tr>
                            <td>
                              <code>{shortIdentity(row.session.session_id)}</code>
                            </td>
                            <td>
                              <code>{shortIdentity(row.task?.task_id)}</code>
                            </td>
                            <td>{row.task?.start_condition ?? "--"}</td>
                            <td>{row.task?.status ?? row.session.state}</td>
                            <td>
                              <span
                                class={`control-deck-state ${readiness()?.state ?? "typed_unknown"}`}
                              >
                                {readiness()?.state ?? "typed_unknown"}
                              </span>
                            </td>
                            <td>
                              <code>{readiness()?.reason_codes[0] ?? "--"}</code>
                            </td>
                            <td>
                              <code>{shortIdentity(readiness()?.scope_claim_sha256)}</code>
                            </td>
                            <td>
                              <code>
                                {readiness()
                                  ? `${readiness()!.active_lease_ids.length} / ${readiness()!.active_runtime_count}:${readiness()!.maximum_parallel_runtime_workers ?? "--"}`
                                  : "--"}
                              </code>
                            </td>
                            <td>{formatTime(row.session.updated_at_ms)}</td>
                          </tr>
                        );
                      }}
                    </For>
                  </tbody>
                </table>
              </div>
            </section>

            <section class="control-deck-band">
              <div class="control-deck-section-title">
                <h2>Callback / Outbox / ACK</h2>
                <span>{value().convergence.length} owners</span>
              </div>
              <div class="control-deck-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Commander</th>
                      <th>Availability</th>
                      <th>Receipts</th>
                      <th>Callbacks</th>
                      <th>Continuations</th>
                      <th>Blocker</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={value().convergence}>
                      {(entry) => {
                        const rows = convergenceRows(entry);
                        return (
                          <tr>
                            <td>
                              <code>{shortIdentity(entry.commander_session_id)}</code>
                            </td>
                            <td>
                              <span class={`control-deck-state ${entry.availability}`}>
                                {entry.availability}
                              </span>
                            </td>
                            <td>{rows.receipts.length}</td>
                            <td>{rows.callbacks.length}</td>
                            <td>{rows.continuations.length}</td>
                            <td>
                              <code>
                                {entry.blocker_code ?? entry.projection?.last_blocker?.code ?? "--"}
                              </code>
                            </td>
                          </tr>
                        );
                      }}
                    </For>
                  </tbody>
                </table>
              </div>
            </section>

            <section class="control-deck-band">
              <div class="control-deck-section-title">
                <h2>Runtime / Lease</h2>
                <span>{value().runtimes.length} runtimes</span>
              </div>
              <div class="control-deck-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Runtime</th>
                      <th>Session</th>
                      <th>State</th>
                      <th>Lease</th>
                      <th>Revision</th>
                      <th>Terminal</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={value().runtimes}>
                      {(runtime) => (
                        <tr>
                          <td>
                            <code>{shortIdentity(runtime.runtime_id)}</code>
                          </td>
                          <td>
                            <code>{shortIdentity(runtime.session_id)}</code>
                          </td>
                          <td>{runtime.runtime_state ?? "--"}</td>
                          <td>
                            <span
                              class={`control-deck-state ${runtime.lease_active ? "active" : "released"}`}
                            >
                              {runtime.lease_active ? "active" : "released"}
                            </span>
                          </td>
                          <td>{runtime.revision}</td>
                          <td>{runtime.terminal ? "yes" : "no"}</td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
            </section>

            <section class="control-deck-band typed-absence">
              <div class="control-deck-section-title">
                <h2>Typed Absence</h2>
                <span>{value().typed_absences.length} explicit</span>
              </div>
              <div class="control-deck-absence-list">
                <For each={value().typed_absences}>
                  {(absence) => (
                    <div>
                      <code>
                        {absence.scope}/{shortIdentity(absence.identity)}
                      </code>
                      <span>{absence.field}</span>
                      <strong>{absence.reason}</strong>
                    </div>
                  )}
                </For>
              </div>
            </section>

            <footer class="control-deck-footer">
              <span>Observed {formatTime(value().observed_at_ms)}</span>
              <code>{value().authority_effect}</code>
            </footer>
          </>
        )}
      </Show>
    </main>
  );
}
