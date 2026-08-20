/*
 * ===============================================================================================================
 *                                Copyright 2021, Blue Yonder Group, Inc.
 *                                           All Rights Reserved
 *
 *                               THIS IS UNPUBLISHED PROPRIETARY SOURCE CODE OF
 *                                          BLUE YONDER GROUP, INC.
 *
 *
 *                         The copyright notice above does not evidence any actual
 *                                 or intended publication of such source code.
 *
 * ===============================================================================================================
 */

import React from 'react';
import ReactDOM from 'react-dom';
import App from './App';
import { EventsHandler } from './components/events-handler/EventsHandler';
import { BrowserRouter } from 'react-router-dom';

const configuredFramePath = window.env.FRAME_URL_PATH || '';
const FRAME_URL_PATH = configuredFramePath.includes('&lt;') ? '/' : configuredFramePath || '/';

ReactDOM.render(
  <EventsHandler>
    <BrowserRouter basename={FRAME_URL_PATH || '/'}>
      <App />
    </BrowserRouter>
  </EventsHandler>,
  document.getElementById('root'),
);
