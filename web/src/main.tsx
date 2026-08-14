import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { AvatarProvider } from "./avatar";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ThemeProvider } from "./theme";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <AvatarProvider>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </AvatarProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
