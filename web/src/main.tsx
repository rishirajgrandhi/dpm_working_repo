import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { Overview } from "./routes/Overview";
import "./styles.css";

// Polling for lists, SSE for live run progress (12 §7). SSE arrives with runs in M1.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

// M0 ships a single screen. Routing arrives with the other screens in M1-M3 (02 D4: a
// thin vertical slice from M1 onward, so the UI is never a big-bang at the end).
const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* The dev project points at the live DPHM_TEST fixture in Snowflake, so every
          screen shows real state rather than placeholders. */}
      <Overview project="dev" />
    </QueryClientProvider>
  </React.StrictMode>,
);
