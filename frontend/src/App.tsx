import { ConfigProvider } from '@arco-design/web-react';
import zhCN from '@arco-design/web-react/es/locale/zh-CN';
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { makeQueryClient } from './app/queryClient';
import { AppRoutes } from './app/routes';
import { routerBasename } from './base';

export function AppProviders({ children, client }: { children: ReactNode; client?: QueryClient }) {
  const [qc] = useState(() => client ?? makeQueryClient());
  return (
    <QueryClientProvider client={qc}>
      <ConfigProvider locale={zhCN} componentConfig={{ Table: { border: false } }}>
        {children}
      </ConfigProvider>
    </QueryClientProvider>
  );
}

export function App() {
  return (
    <AppProviders>
      <BrowserRouter basename={routerBasename()}>
        <AppRoutes />
      </BrowserRouter>
    </AppProviders>
  );
}
