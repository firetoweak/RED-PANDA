import { configureStore } from "@reduxjs/toolkit";

import { redpandaApi } from "../api/redpandaApi";
import runtimeReducer from "../realtime/runtimeSlice";

export const store = configureStore({
  reducer: {
    [redpandaApi.reducerPath]: redpandaApi.reducer,
    runtime: runtimeReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware().concat(redpandaApi.middleware),
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
