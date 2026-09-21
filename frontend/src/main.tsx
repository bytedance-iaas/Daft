import React from 'react';
import ReactDOM from 'react-dom/client';
import '@arco-design/web-react/dist/css/arco.css';
import './styles.css';
import { App } from './App';

declare const __CURATOR_MOCKS__: boolean;

async function bootstrap() {
  // Dev (without VITE_API_TARGET) and demo builds answer the API from the mock world. The branch is
  // compiled away in production builds, so no mock code ships.
  if (__CURATOR_MOCKS__) {
    const { startMockWorker } = await import('./mocks/browser');
    await startMockWorker();
  }
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

void bootstrap();
