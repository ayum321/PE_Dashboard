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
import { Box, makeStyles, Paper, Theme, Typography } from '@material-ui/core';
import { CentralZone, EastZone, LayoutWrapper, NorthZone, WestZone } from '@jda/lui-dashboard-scaffolding-layouts';
import { LuiLogoStacked } from '@jda/lui-common-component-library';

const useStyles = makeStyles((theme: Theme) => {
  return {
    welcomeContainer: {
      paddingLeft: theme.spacing(1),
      paddingRight: theme.spacing(1),
    },
    welcomePaper: {
      width: '100%',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: 'center',
      paddingTop: theme.spacing(10),
      paddingBottom: theme.spacing(10),
      height: `calc(100vh - ${theme.spacing(50.75)}px)`,
      marginBottom: theme.spacing(1),
      overflow: 'hidden',
    },
    logoContainer: {
      width: theme.spacing(60),
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: 'center',
    },
    welcomeMessageTitle: {
      paddingBottom: theme.spacing(5),
    },
    paperWestZone: {
      height: `calc(100vh - ${theme.spacing(30.75)}px)`,
    },
    paperEastZone: {
      height: `calc(100vh - ${theme.spacing(30.75)}px)`,
    },
    paperNorthZone: {
      height: `${theme.spacing(12.5)}px`,
    },
  };
});

const luiLogoStackedStyles = { height: 350, width: 350 };

export function Welcome() {
  const { LOCAL_APP_NAME } = window.env;
  const classes = useStyles();

  return (
    <div className={classes.welcomeContainer}>
      <LayoutWrapper>
        {/* 
        // @ts-ignore */}
        <NorthZone title={LOCAL_APP_NAME} stickyData="Sticky data hidden until scroll" isHidden={false} isSticky={true}>
          <Paper className={classes.paperNorthZone}></Paper>
        </NorthZone>
        <WestZone isHidden={false} isCollapsed={false} isSticky={false}>
          <Paper className={classes.paperWestZone}></Paper>
        </WestZone>
        <CentralZone>
          <Paper className={classes.welcomePaper} component="div">
            <Box className={classes.logoContainer} component="div">
              <LuiLogoStacked style={luiLogoStackedStyles} />
            </Box>
            <Typography variant="h3" component="div">
              <Box>
                Edit <code>src/components/welcome/Welcome.tsx</code> and save to reload.
              </Box>
            </Typography>
          </Paper>
        </CentralZone>
        <EastZone isHidden={false} isCollapsed={false} isSticky={false}>
          <Paper className={classes.paperEastZone}></Paper>
        </EastZone>
      </LayoutWrapper>
    </div>
  );
}
