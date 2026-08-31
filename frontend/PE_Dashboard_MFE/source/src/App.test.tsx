/*
 * ===============================================================================================================
 *                                Copyright 2023, Blue Yonder Group, Inc.
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
import { render } from '@testing-library/react';
import { createMemoryHistory } from 'history';
import { Router } from 'react-router-dom';
import App from './App';

describe('App', () => {
  it('should render correctly', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const history = createMemoryHistory();

    const { getByTestId } = render(
      <Router history={history}>
        <App />
      </Router>,
    );

    expect(getByTestId('lui-ccl-background')).toBeDefined();
  });
});
