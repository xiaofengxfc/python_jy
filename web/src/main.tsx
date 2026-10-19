import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

const host = document.getElementById("root");
if (!host) throw new Error("#root 不存在");

createRoot(host).render(<App />);
