/** Runtime OIDC configuration and the in-memory Keycloak session. */
import axios from "axios";
import Keycloak from "keycloak-js";

import type { AppConfig } from "@/api/system";

let identity: Keycloak | undefined;
let initialization: Promise<Keycloak> | undefined;
let refreshing: Promise<boolean> | undefined;

export function initializeIdentity(): Promise<Keycloak> {
  if (!initialization) {
    initialization = (async () => {
      // Clear credentials left by versions that used local password login.
      localStorage.removeItem("root_transcribe.access_token");
      localStorage.removeItem("root_transcribe.refresh_token");
      const { data } = await axios.get<AppConfig>("/api/v1/config");
      const client = new Keycloak({
        url: data.keycloak_url,
        realm: data.keycloak_realm,
        clientId: data.keycloak_client_id,
      });
      await client.init({
        onLoad: "check-sso",
        pkceMethod: "S256",
        checkLoginIframe: false,
        silentCheckSsoRedirectUri: `${window.location.origin}/silent-check-sso.html`,
        silentCheckSsoFallback: false,
      });
      identity = client;
      return client;
    })().catch((error: unknown) => {
      initialization = undefined;
      throw error;
    });
  }
  return initialization;
}

export async function getAccessToken(
  force = false,
): Promise<string | undefined> {
  if (!identity?.authenticated) return undefined;
  if (!refreshing) {
    refreshing = identity.updateToken(force ? -1 : 30).finally(() => {
      refreshing = undefined;
    });
  }
  await refreshing;
  return identity.token;
}

export function clearIdentity(): void {
  identity?.clearToken();
}

export async function loginIdentity(path = "/workspaces"): Promise<void> {
  const client = await initializeIdentity();
  // The return destination must stay on this origin.
  const target = new URL(path, window.location.origin);
  const redirectUri =
    target.origin === window.location.origin
      ? target.href
      : `${window.location.origin}/workspaces`;
  await client.login({ redirectUri });
}

export async function logoutIdentity(): Promise<void> {
  const client = await initializeIdentity();
  const url = client.createLogoutUrl({
    redirectUri: `${window.location.origin}/login`,
  });
  client.clearToken();
  window.location.assign(url);
}

export async function manageIdentity(): Promise<void> {
  const client = await initializeIdentity();
  await client.accountManagement();
}
