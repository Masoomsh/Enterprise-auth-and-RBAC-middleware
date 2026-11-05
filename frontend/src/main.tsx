import React from "react";
import ReactDOM from "react-dom/client";
import SuperTokens, { SuperTokensWrapper } from "supertokens-auth-react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import Session from "supertokens-auth-react/recipe/session";
import App from "./App";
import "./index.css";

const apiDomain = import.meta.env.VITE_API_DOMAIN;
const websiteDomain = import.meta.env.VITE_WEBSITE_DOMAIN;

if (!apiDomain || !websiteDomain) {
  throw new Error("VITE_API_DOMAIN and VITE_WEBSITE_DOMAIN must be configured.");
}

SuperTokens.init({
  appInfo: {
    appName: "Enterprise Auth RBAC",
    apiDomain,
    websiteDomain,
    apiBasePath: "/auth",
    websiteBasePath: "/auth",
  },
  recipeList: [EmailPassword.init(), Session.init()],
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <SuperTokensWrapper>
      <App />
    </SuperTokensWrapper>
  </React.StrictMode>,
);
