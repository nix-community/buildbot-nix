{
  /**
    Marks a string whose placeholders the service substitutes when
    running post-build steps. Supported placeholders are
    `%(prop:NAME)s` (build properties such as attr, out_path,
    project; see post_build.build_props) and `%(secret:NAME)s`
    (files in the systemd credentials directory).

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
