// Fonts ship with the dashboard: no requests to third-party font servers.
import '@fontsource-variable/instrument-sans';
import '@fontsource-variable/newsreader/opsz.css';
import '@fontsource-variable/newsreader/opsz-italic.css';
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './style.css';

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
