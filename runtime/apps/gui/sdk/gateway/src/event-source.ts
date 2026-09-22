import type { ControlDeckChange, GatewayEventEnvelope } from "./types";

export type GatewayEventHandler = (event: GatewayEventEnvelope) => void;
export type GatewayEventErrorHandler = (error: Event | Error) => void;

export type GatewayEventStream = {
  close: () => void;
};

export function connectControlDeckEvents(input: {
  baseUrl: string;
  onChange: (event: ControlDeckChange) => void;
  onUnavailable?: (blockerCode: string) => void;
  onError?: GatewayEventErrorHandler;
}): GatewayEventStream {
  if (typeof EventSource === "undefined") {
    input.onError?.(new Error("EventSource is not available"));
    return { close: () => undefined };
  }
  const eventSource = new EventSource(new URL("/control-deck/events", input.baseUrl).toString());
  eventSource.addEventListener("control_deck_changed", (message) => {
    try {
      input.onChange(JSON.parse((message as MessageEvent<string>).data) as ControlDeckChange);
    } catch (error) {
      input.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  });
  eventSource.addEventListener("control_deck_unavailable", (message) => {
    try {
      const payload = JSON.parse((message as MessageEvent<string>).data) as {
        blocker_code?: string;
      };
      input.onUnavailable?.(payload.blocker_code ?? "CONTROL_DECK_UNAVAILABLE");
    } catch (error) {
      input.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  });
  eventSource.onerror = (event) => input.onError?.(event);
  return { close: () => eventSource.close() };
}

export function connectGatewayEvents(input: {
  baseUrl: string;
  sessionID?: string;
  onEvent: GatewayEventHandler;
  onError?: GatewayEventErrorHandler;
}): GatewayEventStream {
  if (typeof EventSource === "undefined") {
    input.onError?.(new Error("EventSource is not available"));
    return { close: () => undefined };
  }

  const path = input.sessionID
    ? `/session/${encodeURIComponent(input.sessionID)}/events`
    : "/event";
  const url = new URL(path, input.baseUrl);
  const eventSource = new EventSource(url.toString());

  eventSource.onmessage = (message) => {
    try {
      const parsed = JSON.parse(message.data) as GatewayEventEnvelope;
      if (parsed && typeof parsed === "object" && "payload" in parsed) {
        input.onEvent(parsed);
      }
    } catch (error) {
      input.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  };

  eventSource.onerror = (event) => {
    input.onError?.(event);
  };

  return {
    close: () => eventSource.close(),
  };
}
