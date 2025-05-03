{
  /**
    Marks a string as a value that will be interpolated on the buildbot master or worker.

    See https://docs.buildbot.net/latest/manual/configuration/properties.html#interpolate
    for details on the semantics of the interpolation format string.

    Note that the `kw` selector (and passing keyword arguments) is not supported.

    # Type
    ```
    interpolate :: String -> InterpolateString
    ```

    # Arguments

    value
    : The interpolation format string.
  */
  interpolate = value: {
    _type = "interpolate";
    inherit value;
  };
}
