/** Mock the external identity adapter, while profile APIs still use MSW. */
import { vi } from "vitest";

export const identitySession = {
  authenticated: false,
  token: undefined as string | undefined,
};
export const identityMock = {
  initializeIdentity: vi.fn(async () => identitySession),
  getAccessToken: vi.fn(async (_force?: boolean) => identitySession.token),
  clearIdentity: vi.fn(() => {
    identitySession.authenticated = false;
    identitySession.token = undefined;
  }),
  loginIdentity: vi.fn(async (_path?: string) => {}),
  logoutIdentity: vi.fn(async () => {
    identitySession.authenticated = false;
    identitySession.token = undefined;
  }),
  manageIdentity: vi.fn(async () => {}),
};

export function resetIdentityMock() {
  identitySession.authenticated = false;
  identitySession.token = undefined;
  Object.values(identityMock).forEach((mock) => mock.mockClear());
}
