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
import { Breadcrumbs as BreadcrumbsComponent } from 'react-breadcrumbs';
import { makeStyles, Theme } from '@material-ui/core';
import { isIFrame } from '@jda/lui-portal-utilities';
import { handleBreadcrumbs } from '../events-handler/EventEmmiters';

const useStyles = makeStyles((theme: Theme) => {
  return {
    breadcrumbHelper: {
      display: 'none',
    },
    bodyText: {
      color: theme.palette.text.primary,
    },
  };
});

export function Breadcrumbs() {
  const classes = useStyles();
  return isIFrame() ? (
    <BreadcrumbsComponent
      className={classes.breadcrumbHelper}
      setCrumbs={(breadcrumbs: any) => {
        return handleBreadcrumbs(breadcrumbs);
      }}
    />
  ) : null;
}
