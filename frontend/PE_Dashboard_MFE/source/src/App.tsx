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

import React, { useContext, useEffect } from 'react';
import { Redirect, Route, Switch } from 'react-router-dom';
import { Box, ThemeProvider } from '@material-ui/core';
import { LuiBackground } from '@jda/lui-common-component-library';
import { useDomHistoryMonitor } from '@jda/lui-portal-utilities';
import { EventContext } from './context';
import { Breadcrumbs } from './components/breadcrumbs/Breadcrumbs';
import { AppDataProvider } from './context/AppDataContext';
import { dashboardTheme } from './theme/dashboardTheme';
import './theme/dashboard.css';
import { Header } from './components/layout/Header';
import { Sidebar } from './components/layout/Sidebar';
import { CustomerAuditBanner } from './components/shared/CustomerAuditBanner';
import { UploadPanel } from './components/panels/UploadPanel';
import { BatchPanel } from './components/panels/BatchPanel';
import { ResourcePanel } from './components/panels/ResourcePanel';
import { SlaMatrixPanel } from './components/panels/SlaMatrixPanel';
import { BenchmarkPanel } from './components/panels/BenchmarkPanel';
import { SowPanel } from './components/panels/SowPanel';
import { FindingsPanel } from './components/panels/FindingsPanel';
import { GovernancePanel } from './components/panels/GovernancePanel';
import { ArchivePanel } from './components/panels/ArchivePanel';
import { SettingsPanel } from './components/panels/SettingsPanel';

function App() {
  useDomHistoryMonitor();
  const { theme } = useContext(EventContext);

  useEffect(() => {
    document.title = 'PE Audit Dashboard';
  }, []);

  return (
    <div className="app">
      <ThemeProvider theme={theme}>
        <LuiBackground>
          <Breadcrumbs />
          <ThemeProvider theme={dashboardTheme}>
          <AppDataProvider>
            <div className="pe-dashboard">
            <Box display="flex" style={{ height: '100vh', overflowY: 'auto' }}>
              <Sidebar />
              <Box flexGrow={1} minWidth={0} style={{ background: '#060914', minHeight: '100vh' }}>
                <Header />
                <Box p={2} pb={0}>
                  <CustomerAuditBanner />
                </Box>
                <Switch>
                  <Route exact path="/upload" component={UploadPanel} />
                  <Route exact path="/batch" component={BatchPanel} />
                  <Route exact path="/resource" component={ResourcePanel} />
                  <Route exact path="/sla-matrix" component={SlaMatrixPanel} />
                  <Route exact path="/benchmark" component={BenchmarkPanel} />
                  <Route exact path="/sow" component={SowPanel} />
                  <Route exact path="/findings" component={FindingsPanel} />
                  <Route exact path="/governance" component={GovernancePanel} />
                  <Route exact path="/archive" component={ArchivePanel} />
                  <Route exact path="/settings" component={SettingsPanel} />
                  <Redirect exact from="/" to="/upload" />
                </Switch>
              </Box>
            </Box>
            </div>
          </AppDataProvider>
          </ThemeProvider>
        </LuiBackground>
      </ThemeProvider>
    </div>
  );
}

export default App;
