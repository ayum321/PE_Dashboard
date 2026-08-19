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

import React, { useContext } from 'react';
import { Route, Switch } from 'react-router-dom';
import { ThemeProvider } from '@material-ui/core';
import { LuiBackground } from '@jda/lui-common-component-library';
import { useDomHistoryMonitor } from '@jda/lui-portal-utilities';
import { Welcome } from './components/welcome/Welcome';
import { EventContext } from './context';
import { Breadcrumbs } from './components/breadcrumbs/Breadcrumbs';

function App() {
  useDomHistoryMonitor();
  const { theme } = useContext(EventContext);

  return (
    <div className="app">
      <ThemeProvider theme={theme}>
        <LuiBackground>
          <Breadcrumbs />
          <Switch>
            <Route exact path="/" component={Welcome} />
          </Switch>
        </LuiBackground>
      </ThemeProvider>
    </div>
  );
}

export default App;
