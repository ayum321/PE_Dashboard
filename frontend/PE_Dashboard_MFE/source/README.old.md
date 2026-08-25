# Getting Started with your MFE

This project was bootstrapped with [Create React App](https://github.com/facebook/create-react-app) and the Luminate Portal Create MFE template

## Available Scripts

---

### By create-react-app

---
### `npm start`

Runs the app in the development mode.\
Open [http://localhost:7777](http://localhost:7777) to view it in the browser within a local portal instance.

The page will reload if you make edits.\
You will also see any lint errors in the console.

### `npm test`

Launches the test runner in the interactive watch mode.\
See the section about [running tests](https://facebook.github.io/create-react-app/docs/running-tests) for more information.

### `npm run build`

Builds the app for production to the `build` folder.\
It correctly bundles React in production mode and optimizes the build for the best performance.

The build is minified and the filenames include the hashes.\
Your app is ready to be deployed!

See the section about [deployment](https://facebook.github.io/create-react-app/docs/deployment) for more information.

### `npm run eject`

**Note: this is a one-way operation. Once you `eject`, you can’t go back!**

If you aren’t satisfied with the build tool and configuration choices, you can `eject` at any time. This command will remove the single build dependency from your project.

Instead, it will copy all the configuration files and the transitive dependencies (webpack, Babel, ESLint, etc) right into your project so you have full control over them. All of the commands except `eject` will still work, but they will point to the copied scripts so you can tweak them. At this point you’re on your own.

You don’t have to ever use `eject`. The curated feature set is suitable for small and middle deployments, and you shouldn’t feel obligated to use this feature. However we understand that this tool wouldn’t be useful if you couldn’t customize it when you are ready for it.

---

### By plat-lui-create-mfe

---

### `start:standalone`

This command runs the application in standalone mode, without the portal shell, so you can use other local version of portal rather than the one that is offered as package

### `test-no-coverage`

This command runs the jest tests on no-coverage mode, by default they report the current coverage

### `lint`

This command runs es-lint throughout the micro frontend files

### `format`

This command runs prettier with custom settings formatting all of the files of the micro frontend

### `clear-jest`

This command cleans the jest cache

### `update-portal-dependencies`

This command updates the current dependencies based on the dependencies of the plat-lui-create-mfe package, hence making available the latest recommended dependencies that go along with the portal