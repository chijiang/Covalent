import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { storedTheme, applyTheme } from "./theme";
import "./styles.css";

applyTheme(storedTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
