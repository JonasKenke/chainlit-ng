import { ChainlitContext } from '@chainlit-ng/react-client';
import AppWrapper from 'AppWrapper';
import { apiClient } from 'api';
import React from 'react';
import ReactDOM from 'react-dom/client';

import { AppStateProvider } from './state/store';

import './index.css';

import { i18nSetupLocalization } from './i18n';

i18nSetupLocalization();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <ChainlitContext.Provider value={apiClient}>
      <AppStateProvider>
        <AppWrapper />
      </AppStateProvider>
    </ChainlitContext.Provider>
  </React.StrictMode>
);
