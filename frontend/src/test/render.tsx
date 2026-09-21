import { render, type RenderResult } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { AppProviders } from '../App';
import { makeQueryClient } from '../app/queryClient';
import { AppRoutes } from '../app/routes';

let lastLocation = '';

function LocationProbe() {
  const loc = useLocation();
  lastLocation = `${loc.pathname}${loc.search}${loc.hash}`;
  return null;
}

/** The current in-app location of the last renderApp() (path + query + hash). */
export function currentLocation(): string {
  return lastLocation;
}

/** Renders the whole app at a route, with a fresh query cache (no retries in tests). */
export function renderApp(route: string): RenderResult & { user: UserEvent } {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 0 }, mutations: { retry: false } });
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  const result = render(
    <AppProviders client={client}>
      <MemoryRouter initialEntries={[route]}>
        <AppRoutes />
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>,
  );
  return { ...result, user };
}

/** Renders a component inside the providers and a router (unit tests of widgets). */
export function renderWithProviders(ui: ReactElement, route = '/'): RenderResult & { user: UserEvent } {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 0 }, mutations: { retry: false } });
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  const result = render(
    <AppProviders client={client}>
      <MemoryRouter initialEntries={[route]}>
        {ui}
        <LocationProbe />
      </MemoryRouter>
    </AppProviders>,
  );
  return { ...result, user };
}
